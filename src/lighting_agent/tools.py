"""Agent tools backed by auditable services, not ad-hoc chat state."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from math import isclose
from pathlib import Path

# langchain_core.tools 的 tool 与 langchain.tools 等价，但导入快约 25 倍：
# langchain.tools 会级联拉起 langgraph.prebuilt → sentence_transformers → torch。
from langchain_core.tools import tool
from pydantic import Field, model_validator

from .calculations import calculate_lumen_method, check_design_rules as run_rule_checks
from .calculations.layout import analyze_luminaire_layout as run_luminaire_layout_analysis
from . import dialux_protocol
from .dialux_api import (
    DialuxAPI,
    DialuxAPIError,
    candidate_summary,
    validate_luminaire_search,
)
from .dialux_protocol import DialuxProtocolError
from .deliverables import build_design_report, build_dialux_task_archive, build_dialux_task_package
from .document_loader import load_document
from .report_parser import LuminaireReportParseError, parse_luminaire_report
from .project_store import ProjectStore, RevisionConflictError
from .photometry_assets import PhotometryAssetStore
from .rag import create_evidence_store, format_evidence
from .schemas import (
    CalculationInput,
    DesignBrief,
    LightingGroup,
    LightingParameterSource,
    LuminaireSearchRun,
    LuminaireSearchRequest,
    ProjectState,
    ProjectUpdate,
    RuleRequirement,
    StrictModel,
)


def _data(value: object) -> dict | list | str | int | float | bool | None:
    """Return JSON-compatible Pydantic data for LangChain tool messages."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return, union-attr]
    return value  # type: ignore[return-value]


class CreateProjectInput(StrictModel):
    brief: DesignBrief


class ProjectReference(StrictModel):
    project_id: str = Field(min_length=8, max_length=64)


class EvidenceAdoptionInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class EvidenceSearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    project_id: str | None = Field(default=None, min_length=8, max_length=64)


class BriefUpdateInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    brief: DesignBrief


class LightingGroupsUpdateInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    lighting_groups: list[LightingGroup] = Field(min_length=1, max_length=100)


class RagLightingParameterInput(ProjectReference):
    """Evidence-backed values extracted from approved RAG results."""

    expected_revision: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    target_illuminance_lx: float | None = Field(default=None, gt=0, le=100_000)
    target_cct_k: int | None = Field(default=None, ge=1_000, le=20_000)
    min_cri: int | None = Field(default=None, ge=0, le=100)
    target_ugr: float | None = Field(default=None, ge=0, le=40)


_CALCULATION_INPUT_ALIASES = {
    "lighting_group_id": "group_id",
    "region": "region_name",
    "zone_name": "region_name",
    "lighting_group_name": "group_name",
    "group": "group_name",
    "mounting_height": "mounting_height_m",
    "mounting_point_height_m": "mounting_height_m",
    "area": "area_m2",
    "target_lx": "target_illuminance_lx",
    "target_lux": "target_illuminance_lx",
    "illuminance_lx": "target_illuminance_lx",
    "lumens": "luminaire_luminous_flux_lm",
    "luminous_flux_lm": "luminaire_luminous_flux_lm",
    "luminaire_flux_lm": "luminaire_luminous_flux_lm",
    "flux_lm": "luminaire_luminous_flux_lm",
    "power": "luminaire_power_w",
    "power_w": "luminaire_power_w",
    "luminaire_power": "luminaire_power_w",
    "uf": "utilization_factor",
    "utilisation_factor": "utilization_factor",
    "utilization": "utilization_factor",
    "mf": "maintenance_factor",
    "maintenance": "maintenance_factor",
    "maintenance_coefficient": "maintenance_factor",
    "selected_luminaire_id": "luminaire_id",
}
_CALCULATION_NUMERIC_FIELDS = frozenset(
    {
        "mounting_height_m",
        "area_m2",
        "target_illuminance_lx",
        "luminaire_luminous_flux_lm",
        "luminaire_power_w",
        "utilization_factor",
        "maintenance_factor",
    }
)
_CALCULATION_REMOVED_FIELDS = frozenset(
    {"target_uniformity_u0", "max_lpd_w_m2", "installed_power_density_w_m2"}
)
_CALCULATION_CANONICAL_FIELDS = frozenset(
    {
        "group_id",
        "region_name",
        "group_name",
        "mounting_height_m",
        "area_m2",
        "target_illuminance_lx",
        "luminaire_luminous_flux_lm",
        "luminaire_power_w",
        "utilization_factor",
        "maintenance_factor",
        "luminaire_id",
    }
)
_NESTED_GROUP_ALIASES = {
    "id": "group_id",
    "name": "group_name",
    "region": "region_name",
    "zone": "region_name",
    "area": "area_m2",
    "mounting_height": "mounting_height_m",
    "mounting_point_height_m": "mounting_height_m",
    "target_lx": "target_illuminance_lx",
    "target_lux": "target_illuminance_lx",
}
_NESTED_LUMINAIRE_ALIASES = {
    "id": "luminaire_id",
    "lumens": "luminaire_luminous_flux_lm",
    "luminous_flux": "luminaire_luminous_flux_lm",
    "luminous_flux_lm": "luminaire_luminous_flux_lm",
    "flux": "luminaire_luminous_flux_lm",
    "flux_lm": "luminaire_luminous_flux_lm",
    "power": "luminaire_power_w",
    "power_w": "luminaire_power_w",
    "wattage": "luminaire_power_w",
}
_NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)")


def _calculation_number(value: object) -> object:
    """Accept provider values such as ``"1200 lm"`` without weakening bounds."""

    if isinstance(value, bool) or not isinstance(value, str):
        return value
    text = value.strip().replace("，", ",")
    # Treat a single comma followed by one or two digits as a decimal comma;
    # commas in larger numbers are thousands separators.
    normalized = text.replace(",", ".") if re.search(r"\d,[0-9]{1,2}(?:\D|$)", text) else text.replace(",", "")
    match = _NUMBER_PATTERN.search(normalized)
    if match is None:
        return value
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return value


def _merge_nested_calculation_fields(
    source: dict[object, object],
    target: dict[str, object],
    aliases: dict[str, str],
) -> None:
    """Extract calculation fields from provider objects and ignore metadata."""

    for raw_key, value in source.items():
        key = str(raw_key)
        normalized = aliases.get(key, key)
        if normalized in _CALCULATION_CANONICAL_FIELDS or normalized in _CALCULATION_INPUT_ALIASES:
            target.setdefault(normalized, value)


class CalculationToolInput(StrictModel):
    """Provider-facing calculation input with backwards-compatible aliases.

    The persisted/domain ``CalculationInput`` remains strict. This adapter is
    intentionally permissive because model providers commonly omit values that
    are already present in the confirmed group or selected luminaire.
    """

    group_id: str = Field(default="unassigned", min_length=1, max_length=64)
    region_name: str = Field(default="Unassigned region", min_length=1, max_length=160)
    group_name: str = Field(default="Unassigned group", min_length=1, max_length=160)
    mounting_height_m: float | None = Field(default=None, gt=0, le=100)
    area_m2: float | None = Field(default=None, gt=0, description="Confirmed group area in m2.")
    target_illuminance_lx: float | None = Field(default=None, gt=0, description="Confirmed target illuminance in lx.")
    luminaire_luminous_flux_lm: float | None = Field(
        default=None, gt=0, description="Luminaire flux in lm; may be read from the selected candidate."
    )
    luminaire_power_w: float | None = Field(
        default=None, gt=0, description="Luminaire power in W; may be read from the selected candidate."
    )
    utilization_factor: float | None = Field(default=None, gt=0, le=1)
    maintenance_factor: float | None = Field(default=None, gt=0, le=1)
    luminaire_id: str | None = Field(
        default=None, min_length=1, max_length=200, description="Saved project luminaire candidate ID."
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_values(cls, values: object) -> object:
        if hasattr(values, "model_dump"):
            return values.model_dump(mode="json")
        if not isinstance(values, dict):
            return values
        cleaned = dict(values)
        for field in _CALCULATION_REMOVED_FIELDS:
            cleaned.pop(field, None)

        nested_group = cleaned.pop("group", None)
        if isinstance(nested_group, dict):
            _merge_nested_calculation_fields(nested_group, cleaned, _NESTED_GROUP_ALIASES)
        elif isinstance(nested_group, str):
            cleaned.setdefault("group_name", nested_group)

        nested = cleaned.pop("luminaire", None)
        if isinstance(nested, dict):
            _merge_nested_calculation_fields(nested, cleaned, _NESTED_LUMINAIRE_ALIASES)
        elif isinstance(nested, str) and "luminaire_id" not in cleaned:
            cleaned["luminaire_id"] = nested

        for alias, field in _CALCULATION_INPUT_ALIASES.items():
            if alias in cleaned:
                cleaned.setdefault(field, cleaned[alias])
                cleaned.pop(alias, None)
        for field in _CALCULATION_NUMERIC_FIELDS:
            if field not in cleaned:
                continue
            original = cleaned[field]
            value = _calculation_number(original)
            if (
                field in {"utilization_factor", "maintenance_factor"}
                and isinstance(original, str)
                and "%" in original
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 1
            ):
                value /= 100
            # Providers sometimes use zero as an unknown optional value. Let
            # the project/group resolver fill it from confirmed data.
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
                value = None
            cleaned[field] = value
        return cleaned


class ProjectCalculationInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    inputs: CalculationToolInput | list[CalculationToolInput]

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_shape(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        cleaned = dict(values)
        for field in _CALCULATION_REMOVED_FIELDS:
            cleaned.pop(field, None)
        raw = cleaned.pop("inputs", None)
        if raw is None:
            for key in ("calculations", "calculation", "calculation_input", "input"):
                if key in cleaned:
                    raw = cleaned.pop(key)
                    break
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                pass
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump(mode="json")
        elif isinstance(raw, list):
            raw = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in raw]
        if isinstance(raw, dict):
            for key in ("calculation", "calculation_input", "parameters"):
                nested = raw.get(key)
                if isinstance(nested, (dict, list)) and len(raw) == 1:
                    raw = nested
                    break
        if raw is not None:
            cleaned["inputs"] = raw
        else:
            calculation_fields = {
                key: cleaned.pop(key)
                for key in list(cleaned)
                if key in _CALCULATION_INPUT_ALIASES
                or key in _CALCULATION_NUMERIC_FIELDS
                or key in {"group_id", "region_name", "group_name", "luminaire_id", "luminaire"}
            }
            if calculation_fields:
                cleaned["inputs"] = calculation_fields
        return cleaned


class RuleCheckInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    requirements: list[RuleRequirement] = Field(min_length=1, max_length=20)
    observations: dict[str, float | int | None]


class LuminaireSearchToolInput(LuminaireSearchRequest):
    project_id: str | None = Field(default=None, min_length=8, max_length=64)
    expected_revision: int | None = Field(default=None, ge=0)


class LuminaireSelectionInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    luminaire_ids: list[str] = Field(default_factory=list, max_length=100)
    group_assignments: dict[str, list[str]] = Field(default_factory=dict)


class LuminaireDetailInput(ProjectReference):
    luminaire_id: str = Field(min_length=1, max_length=200)


class SendToDialuxInput(ProjectReference):
    luminaire_id: str = Field(min_length=1, max_length=200)


class ClarificationOption(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=200)


class ClarificationField(StrictModel):
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    input_type: str = Field(default="text", pattern=r"^(text|number|select|multiselect)$")
    required: bool = True
    placeholder: str | None = Field(default=None, max_length=160)
    options: list[ClarificationOption] = Field(default_factory=list, max_length=8)


class AskUserInput(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=800)
    fields: list[ClarificationField] = Field(min_length=1, max_length=6)


class DialuxTaskInput(ProjectReference):
    expected_revision: int = Field(ge=0)


class ReportInput(DialuxTaskInput):
    pass


class LuminaireLayoutAnalysisInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    report_file: str = Field(min_length=1, max_length=500)
    coordinate_tolerance_m: float = Field(default=0.05, gt=0, le=10)


project_store = ProjectStore()
evidence_store = create_evidence_store()


def configure_runtime_services(*, projects, evidence) -> None:
    """Bind browser requests to their active persistence services."""

    global project_store, evidence_store
    project_store = projects
    evidence_store = evidence


def _project_directory(project_id: str) -> Path:
    directory_for = getattr(project_store, "directory_for", None)
    if callable(directory_for):
        return directory_for(project_id)
    return project_store.directory


def _artifact_path(project_id: str, suffix: str) -> Path:
    artifact_path = getattr(project_store, "artifact_path", None)
    if callable(artifact_path):
        return artifact_path(project_id, suffix)
    return _project_directory(project_id) / f"{project_id}{suffix}"


def _get_scoped_evidence(evidence_ids: list[str], project_id: str) -> list:
    try:
        return evidence_store.get_evidence(evidence_ids, project_id=project_id)
    except TypeError as error:
        if "project_id" not in str(error):
            raise
        # Keep lightweight test doubles and third-party stores compatible.
        return evidence_store.get_evidence(evidence_ids)


def _update_at_latest_revision(
    project_id: str,
    expected_revision: int,
    build_update: Callable[[ProjectState], ProjectUpdate],
) -> tuple[ProjectState, bool]:
    """Apply an Agent mutation to the newest snapshot, retrying a racing write.

    Agent turns often make several writes in sequence.  The model can retain
    the revision returned by its first read while an earlier tool in that same
    turn has already advanced the project.  These tools modify one isolated
    field or append records, so they can safely rebuild their update from the
    latest state without weakening the strict optimistic-locking API used by
    direct browser edits.
    """

    rebased = False
    last_conflict: RevisionConflictError | None = None
    for _ in range(3):
        current = project_store.get(project_id)
        rebased = rebased or current.revision != expected_revision
        try:
            return project_store.update(project_id, build_update(current)), rebased
        except RevisionConflictError as error:
            last_conflict = error
    assert last_conflict is not None
    raise last_conflict


@tool("create_project", args_schema=CreateProjectInput)
def create_project(brief: DesignBrief) -> dict:
    """Create a versioned lighting-design project from confirmed design inputs."""

    return _data(project_store.create(brief))


@tool("get_project", args_schema=ProjectReference)
def get_project(project_id: str) -> dict:
    """Read the current confirmed brief, evidence, calculations and open questions."""

    return _data(project_store.get(project_id))


@tool("update_project_brief", args_schema=BriefUpdateInput)
def update_project_brief(project_id: str, expected_revision: int, brief: DesignBrief) -> dict:
    """Save confirmed project conditions using optimistic revision control."""

    # A complete task brief is replacement data rather than an append-only
    # record. Keep its optimistic lock strict so an outdated chat turn cannot
    # overwrite conditions just confirmed in another browser session.
    updated = project_store.update(project_id, ProjectUpdate(expected_revision=expected_revision, brief=brief))
    return {**updated.model_dump(mode="json"), "project_revision": updated.revision, "rebased": False}


@tool("update_lighting_groups", args_schema=LightingGroupsUpdateInput)
def update_lighting_groups(
    project_id: str, expected_revision: int, lighting_groups: list[LightingGroup]
) -> dict:
    """Save explicit, user-confirmed region lighting groups and their mounting heights."""

    state = project_store.get(project_id)
    brief = state.brief.model_copy(update={"lighting_groups": lighting_groups})
    updated = project_store.update(
        project_id,
        ProjectUpdate(expected_revision=expected_revision, brief=brief),
    )
    return {
        "lighting_groups": [item.model_dump(mode="json") for item in updated.brief.lighting_groups],
        "project_revision": updated.revision,
        "rebased": False,
    }


@tool("apply_rag_lighting_parameters", args_schema=RagLightingParameterInput)
def apply_rag_lighting_parameters(
    project_id: str,
    expected_revision: int,
    evidence_ids: list[str],
    target_illuminance_lx: float | None = None,
    target_cct_k: int | None = None,
    min_cri: int | None = None,
    target_ugr: float | None = None,
) -> dict:
    """Persist RAG-derived lighting targets with field-level evidence provenance.

    Only use values explicitly supported by the retrieved evidence and
    applicable to the current room/use. Existing manually or document-set
    values are never overwritten by this tool; identical values still receive
    evidence provenance.
    """

    updates = {
        name: value
        for name, value in {
            "target_illuminance_lx": target_illuminance_lx,
            "target_cct_k": target_cct_k,
            "min_cri": min_cri,
            "target_ugr": target_ugr,
        }.items()
        if value is not None
    }
    if not updates:
        raise ValueError("At least one RAG-derived lighting parameter is required")

    evidence_ids = list(dict.fromkeys(evidence_ids))
    evidence = _get_scoped_evidence(evidence_ids, project_id)
    if len(evidence) != len(evidence_ids):
        raise ValueError("Some RAG evidence IDs could not be resolved")

    applied_fields: list[str] = []

    def build_update(current: ProjectState) -> ProjectUpdate:
        nonlocal applied_fields
        sources = dict(current.brief.lighting_parameter_sources)
        applicable_updates: dict[str, float | int] = {}
        protected_fields: list[str] = []
        for name, value in updates.items():
            current_value = getattr(current.brief, name)
            current_source = sources.get(name)
            if current_value is None or current_source is not None or current_value == value:
                applicable_updates[name] = value
            else:
                protected_fields.append(name)
        if protected_fields:
            raise ValueError(
                "RAG cannot overwrite manually or document-confirmed parameters: "
                + ", ".join(protected_fields)
            )
        applied_fields = sorted(applicable_updates)

        for name in applicable_updates:
            sources[name] = LightingParameterSource(evidence_ids=evidence_ids)
        brief = current.brief.model_copy(
            update={
                **applicable_updates,
                "confirmed_fields": current.brief.confirmed_fields | set(applicable_updates),
                "lighting_parameter_sources": sources,
            }
        )
        saved_evidence_ids = {item.evidence_id for item in current.evidence}
        return ProjectUpdate(
            expected_revision=current.revision,
            brief=brief,
            evidence=[
                *current.evidence,
                *(item for item in evidence if item.evidence_id not in saved_evidence_ids),
            ],
        )

    updated, rebased = _update_at_latest_revision(project_id, expected_revision, build_update)
    return {
        "status": "ok",
        "source": "rag",
        "applied_fields": applied_fields,
        "evidence_ids": evidence_ids,
        "project_revision": updated.revision,
        "rebased": rebased,
    }


@tool("search_evidence", args_schema=EvidenceSearchInput)
def search_evidence(query: str, project_id: str | None = None) -> dict:
    """Search global standards plus documents belonging to the requested project."""

    evidence = evidence_store.search(query, top_k=3, project_id=project_id)
    return {"evidence": [_data(item) for item in evidence], "formatted": format_evidence(evidence)}


@tool("adopt_evidence", args_schema=EvidenceAdoptionInput)
def adopt_evidence(project_id: str, expected_revision: int, evidence_ids: list[str]) -> dict:
    """Attach retrieved evidence to a project revision before using it in a formal report."""

    adopted = _get_scoped_evidence(evidence_ids, project_id)
    updated, rebased = _update_at_latest_revision(
        project_id,
        expected_revision,
        lambda current: ProjectUpdate(
            expected_revision=current.revision,
            evidence=[
                *current.evidence,
                *(item for item in adopted if item.evidence_id not in {saved.evidence_id for saved in current.evidence}),
            ],
        ),
    )
    return {
        "evidence": [_data(item) for item in adopted],
        "project_revision": updated.revision,
        "rebased": rebased,
    }


class AddDocumentInput(StrictModel):
    file_path: str
    source_type: str = "project_document"
    project_id: str | None = Field(default=None, min_length=8, max_length=64)


@tool("add_document", args_schema=AddDocumentInput)
def add_document(file_path: str, source_type: str = "project_document", project_id: str | None = None) -> dict:
    """Index an approved workspace .pdf, .docx, .md or .txt document for evidence retrieval."""

    if source_type not in {"standard", "project_document", "user_note"}:
        raise ValueError("source_type must be standard, project_document or user_note")
    if source_type == "project_document" and not project_id:
        raise ValueError("project_document requires project_id")
    document = load_document(file_path)
    chunk_count = evidence_store.add_document(document, source_type=source_type, project_id=project_id)
    return {
        "source_name": document.source_name,
        "sha256": document.sha256,
        "page_count": document.page_count,
        "indexed_chunks": chunk_count,
    }


_PLACEHOLDER_GROUP_IDS = frozenset({"", "unassigned", "default", "general", "general_lighting"})
_PLACEHOLDER_GROUP_NAMES = frozenset({"", "unassigned region", "unassigned group"})


class CalculationInputIncompleteError(ValueError):
    """A recoverable calculation request that needs user/project data."""

    def __init__(self, group_id: str, missing_fields: list[str]) -> None:
        self.group_id = group_id
        self.missing_fields = tuple(missing_fields)
        super().__init__(
            f"Calculation input for lighting group {group_id} is incomplete; "
            "provide or confirm: "
            + ", ".join(missing_fields)
        )


def _text_key(value: object) -> str:
    return str(value or "").strip().casefold()


def _resolve_calculation_group(
    item: CalculationToolInput, groups: dict[str, LightingGroup]
) -> LightingGroup | None:
    """Resolve provider-friendly group references without guessing between groups."""

    if not groups:
        # Projects created before lighting groups were introduced keep the
        # original flat calculation workflow. They are still valid for the
        # lumen-method estimate, which does not use geometry or mounting height.
        if _text_key(item.group_id) not in _PLACEHOLDER_GROUP_IDS:
            raise ValueError(
                "Calculation inputs reference unknown lighting groups: " + item.group_id
            )
        return None

    exact = groups.get(item.group_id)
    if exact is not None:
        return exact

    requested_region = _text_key(item.region_name)
    requested_group = _text_key(item.group_name)
    candidates = [
        group
        for group in groups.values()
        if (
            requested_region not in _PLACEHOLDER_GROUP_NAMES
            and requested_region == _text_key(group.region_name)
        )
        or (
            requested_group not in _PLACEHOLDER_GROUP_NAMES
            and requested_group == _text_key(group.group_name)
        )
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError(
            "Calculation input matches more than one lighting group; use the exact group_id"
        )
    if _text_key(item.group_id) in _PLACEHOLDER_GROUP_IDS and len(groups) == 1:
        return next(iter(groups.values()))
    raise ValueError(
        "Calculation inputs reference unknown lighting groups: "
        + (item.group_id or "(empty)")
    )


def _candidate_values_for_calculation(
    item: CalculationToolInput,
    state: ProjectState,
    group: LightingGroup | None,
) -> list[tuple[float | None, float | None]]:
    """Return saved candidate flux/power pairs relevant to one calculation.

    A group assignment is authoritative.  Falling back to every selected
    luminaire when an assignment exists makes a multi-group project appear
    ambiguous even though each group has a single, explicit fixture.
    """

    ids: list[str] = []
    if item.luminaire_id:
        ids.append(item.luminaire_id)
    elif group is not None:
        assigned_ids = [
            *group.luminaire_ids,
            *state.luminaire_group_assignments.get(group.group_id, []),
        ]
        # An explicit assignment narrows the calculation to this group.  The
        # project-wide selection is only a fallback for legacy/single-group
        # projects that predate group assignments.
        ids.extend(assigned_ids or state.selected_luminaire_ids)
    else:
        ids.extend(state.selected_luminaire_ids)
    ids = list(dict.fromkeys(ids))
    candidates = {candidate.luminaire_id: candidate for candidate in state.luminaires}
    return [
        (candidates[luminaire_id].luminous_flux_lm, candidates[luminaire_id].power_w)
        for luminaire_id in ids
        if luminaire_id in candidates
    ]


def _prepare_calculation_input(
    item: CalculationToolInput | CalculationInput,
    *,
    state: ProjectState,
    groups: dict[str, LightingGroup],
) -> CalculationInput:
    """Fill omitted provider fields from confirmed groups and saved luminaires."""

    if not isinstance(item, CalculationToolInput):
        item = CalculationToolInput.model_validate(
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        )
    group = _resolve_calculation_group(item, groups)
    if group is not None and not group.confirmed:
        raise ValueError(
            "Lighting groups must be user-confirmed before calculation: " + group.group_id
        )

    group_label = group.group_id if group is not None else item.group_id
    region_name = (
        group.region_name
        if group is not None and _text_key(item.region_name) in _PLACEHOLDER_GROUP_NAMES
        else item.region_name
    )
    group_name = (
        group.group_name
        if group is not None and _text_key(item.group_name) in _PLACEHOLDER_GROUP_NAMES
        else item.group_name
    )
    mounting_height = item.mounting_height_m
    if group is not None:
        if mounting_height is None:
            mounting_height = group.mounting_height_m
        elif not isclose(mounting_height, group.mounting_height_m, rel_tol=0, abs_tol=1e-6):
            raise ValueError(
                "Calculation mounting point height must match the confirmed lighting group: "
                + group.group_id
            )

    area = item.area_m2 if item.area_m2 is not None else group.area_m2 if group is not None else state.brief.area_m2
    target = (
        item.target_illuminance_lx
        if item.target_illuminance_lx is not None
        else group.target_illuminance_lx
        if group is not None
        else state.brief.target_illuminance_lx
    )
    utilization = (
        item.utilization_factor
        if item.utilization_factor is not None
        else group.utilization_factor
        if group is not None
        else None
    )
    maintenance = (
        item.maintenance_factor
        if item.maintenance_factor is not None
        else group.maintenance_factor
        if group is not None
        else None
    )
    flux = item.luminaire_luminous_flux_lm
    power = item.luminaire_power_w
    candidate_values = _candidate_values_for_calculation(item, state, group)
    usable_values = [
        (float(candidate_flux), float(candidate_power))
        for candidate_flux, candidate_power in candidate_values
        if candidate_flux is not None
        and candidate_power is not None
        and candidate_flux > 0
        and candidate_power > 0
    ]
    if usable_values:
        distinct_values = list(dict.fromkeys(usable_values))
        if len(distinct_values) == 1 or len(usable_values) == 1:
            candidate_flux, candidate_power = distinct_values[0]
            flux = flux if flux is not None else candidate_flux
            power = power if power is not None else candidate_power

    missing = [
        name
        for name, value in (
            ("area_m2", area),
            ("target_illuminance_lx", target),
            ("luminaire_luminous_flux_lm", flux),
            ("luminaire_power_w", power),
            ("utilization_factor", utilization),
            ("maintenance_factor", maintenance),
        )
        if value is None
    ]
    if missing:
        raise CalculationInputIncompleteError(group_label, missing)
    return CalculationInput(
        group_id=group_label,
        region_name=region_name,
        group_name=group_name,
        mounting_height_m=mounting_height,
        area_m2=area,
        target_illuminance_lx=target,
        luminaire_luminous_flux_lm=flux,
        luminaire_power_w=power,
        utilization_factor=utilization,
        maintenance_factor=maintenance,
    )


@tool("calculate_preliminary_lighting", args_schema=ProjectCalculationInput)
def calculate_preliminary_lighting(
    project_id: str,
    expected_revision: int,
    inputs: CalculationToolInput | list[CalculationToolInput],
) -> dict:
    """Run and persist a reproducible lumen-method calculation.

    Inputs may reference a confirmed lighting group by ID and a saved
    luminaire by ID. Omitted group metadata and luminaire flux/power are filled
    only when the project contains an unambiguous confirmed source; no design
    values are guessed.
    """

    if isinstance(inputs, (CalculationInput, CalculationToolInput)):
        normalized_inputs = [inputs]
    else:
        normalized_inputs = list(inputs)
    if not normalized_inputs:
        raise ValueError("At least one lighting-group calculation input is required")

    state = project_store.get(project_id)
    groups = {group.group_id: group for group in state.brief.lighting_groups}
    try:
        prepared_inputs = [
            _prepare_calculation_input(item, state=state, groups=groups) for item in normalized_inputs
        ]
    except CalculationInputIncompleteError as error:
        return {
            "status": "needs_clarification",
            "group_id": error.group_id,
            "missing_fields": list(error.missing_fields),
            "project_revision": state.revision,
            "message": (
                "The calculation was not run because these confirmed inputs are missing. "
                "Do not guess them. Ask the user to provide or confirm the listed values, "
                "or select a saved luminaire with complete flux and power data."
            ),
        }
    results = [calculate_lumen_method(item) for item in prepared_inputs]
    updated, rebased = _update_at_latest_revision(
        project_id,
        expected_revision,
        lambda current: ProjectUpdate(
            expected_revision=current.revision,
            calculations=[*current.calculations, *results],
        ),
    )
    return {
        "calculations": [_data(result) for result in results],
        "project_revision": updated.revision,
        "rebased": rebased,
    }


@tool("check_design_rules", args_schema=RuleCheckInput)
def check_design_rules(
    project_id: str,
    expected_revision: int,
    requirements: list[RuleRequirement],
    observations: dict[str, float | int | None],
) -> dict:
    """Deterministically compare explicit evidence-derived rules with observed values and save results."""

    checks = run_rule_checks(requirements, observations)
    updated, rebased = _update_at_latest_revision(
        project_id,
        expected_revision,
        lambda current: ProjectUpdate(
            expected_revision=current.revision,
            rule_checks=[*current.rule_checks, *checks],
        ),
    )
    return {
        "checks": [_data(item) for item in checks],
        "project_revision": updated.revision,
        "rebased": rebased,
    }


def _luminaire_request(
    keyword: str,
    lighting_group_id: str | None,
    region_name: str | None,
    mounting_height_m: float | None,
    language: str,
    brand: str | None,
    brand_id: str | None,
    preferred_brands: list[str] | None,
    target_illuminance_lx: float | None,
    target_cct_k: int | None,
    target_cct_tolerance_k: int,
    min_cri: int | None,
    max_ugr: float | None,
    max_power_w: float | None,
    min_ip_rating: str | None,
    max_results: int,
) -> LuminaireSearchRequest:
    return LuminaireSearchRequest(
        keyword=keyword,
        lighting_group_id=lighting_group_id,
        region_name=region_name,
        mounting_height_m=mounting_height_m,
        language=language,
        brand=brand,
        brand_id=brand_id,
        preferred_brands=preferred_brands or [],
        target_illuminance_lx=target_illuminance_lx,
        target_cct_k=target_cct_k,
        target_cct_tolerance_k=target_cct_tolerance_k,
        min_cri=min_cri,
        max_ugr=max_ugr,
        max_power_w=max_power_w,
        min_ip_rating=min_ip_rating,
        max_results=max_results,
    )


def _prepare_luminaire_request(
    request: LuminaireSearchRequest, project_id: str | None
) -> tuple[LuminaireSearchRequest, list[str], ProjectState | None]:
    state = project_store.get(project_id) if project_id is not None else None
    effective, missing = validate_luminaire_search(request, state.brief if state is not None else None)
    return effective, missing, state


@tool("prepare_luminaire_search", args_schema=LuminaireSearchToolInput)
def prepare_luminaire_search(
    keyword: str,
    lighting_group_id: str | None = None,
    region_name: str | None = None,
    mounting_height_m: float | None = None,
    language: str = "zh",
    brand: str | None = None,
    brand_id: str | None = None,
    preferred_brands: list[str] | None = None,
    target_illuminance_lx: float | None = None,
    target_cct_k: int | None = None,
    target_cct_tolerance_k: int = 0,
    min_cri: int | None = None,
    max_ugr: float | None = None,
    max_power_w: float | None = None,
    min_ip_rating: str | None = None,
    max_results: int = 5,
    project_id: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Validate deterministic search prerequisites before any DIALux request."""

    request = _luminaire_request(
        keyword,
        lighting_group_id,
        region_name,
        mounting_height_m,
        language,
        brand,
        brand_id,
        preferred_brands,
        target_illuminance_lx,
        target_cct_k,
        target_cct_tolerance_k,
        min_cri,
        max_ugr,
        max_power_w,
        min_ip_rating,
        max_results,
    )
    effective, missing, state = _prepare_luminaire_request(request, project_id)
    return {
        "status": "ready" if not missing else "needs_clarification",
        "missing_fields": missing,
        "request": _data(effective),
        "project_revision": state.revision if state is not None else None,
        "expected_revision": expected_revision,
    }


@tool("search_luminaires", args_schema=LuminaireSearchToolInput)
def search_luminaires(
    keyword: str,
    lighting_group_id: str | None = None,
    region_name: str | None = None,
    mounting_height_m: float | None = None,
    language: str = "zh",
    brand: str | None = None,
    brand_id: str | None = None,
    preferred_brands: list[str] | None = None,
    target_illuminance_lx: float | None = None,
    target_cct_k: int | None = None,
    target_cct_tolerance_k: int = 0,
    min_cri: int | None = None,
    max_ugr: float | None = None,
    max_power_w: float | None = None,
    min_ip_rating: str | None = None,
    max_results: int = 5,
    project_id: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Find traceable DIALux candidates after deterministic input validation.

    Key conditions are target illuminance, CCT, CRI and UGR (filled from the
    confirmed brief). Power, IP and brand conditions are honoured only when
    the caller states them explicitly.
    """

    request = _luminaire_request(
        keyword,
        lighting_group_id,
        region_name,
        mounting_height_m,
        language,
        brand,
        brand_id,
        preferred_brands,
        target_illuminance_lx,
        target_cct_k,
        target_cct_tolerance_k,
        min_cri,
        max_ugr,
        max_power_w,
        min_ip_rating,
        max_results,
    )
    request, missing, state = _prepare_luminaire_request(request, project_id)
    if missing:
        return {
            "status": "needs_clarification",
            "missing_fields": missing,
            "request": _data(request),
        }
    if project_id is not None and expected_revision is None:
        raise ValueError("expected_revision is required when saving candidates to a project")
    client = DialuxAPI()
    try:
        search_with_run = getattr(client, "search_with_run", None)
        if callable(search_with_run):
            search_result = search_with_run(
                request,
                project_id=project_id,
                project_revision=state.revision if state is not None else None,
            )
            candidates = search_result.candidates
            search_run = search_result.search_run
        else:
            candidates = client.search(request)
            search_run = LuminaireSearchRun(
                project_id=project_id,
                project_revision=state.revision if state is not None else None,
                request=request,
                original_keyword=request.keyword,
                resolved_keyword=request.keyword,
                endpoint="compatibility://dialux-search",
                candidate_ids=[item.luminaire_id for item in candidates],
                detail_status_by_id={item.luminaire_id: item.detail_status for item in candidates},
            )
    except DialuxAPIError as error:
        return {"status": "vendor_error", "vendor_error": error.as_dict()}
    result: dict = {
        "status": "ok",
        "candidates": [candidate_summary(item) for item in candidates],
        "search_run": _data(search_run),
        "notice": "候选灯具需在 DIALux evo 结合空间、反射比、安装高度和布灯方式核验照度及 UGR。",
    }
    if project_id is not None:
        updated, saved_count, rebased = project_store.append_luminaires(
            project_id,
            expected_revision,
            candidates,
            search_run,
        )
        saved_by_id = {item.luminaire_id: item for item in updated.luminaires}
        returned_ids = [item.luminaire_id for item in candidates]
        saved_candidates = [
            saved_by_id[luminaire_id]
            for luminaire_id in returned_ids
            if luminaire_id in saved_by_id
        ]
        excluded_ids = [
            luminaire_id for luminaire_id in returned_ids if luminaire_id not in saved_by_id
        ]
        # Only expose IDs that can be read again through get_luminaire_detail.
        # This keeps an LLM from attempting to dereference a transient vendor
        # search result that was intentionally excluded from project storage.
        result["candidates"] = [candidate_summary(item) for item in saved_candidates]
        result["saved_candidate_ids"] = [item.luminaire_id for item in saved_candidates]
        if excluded_ids:
            result["excluded_candidate_ids"] = excluded_ids
        result["project_revision"] = updated.revision
        result["saved_count"] = saved_count
        result["rebased"] = rebased
    return result


@tool("get_luminaire_detail", args_schema=LuminaireDetailInput)
def get_luminaire_detail(project_id: str, luminaire_id: str) -> dict:
    """Read bounded saved supplier detail for one candidate after shortlist comparison."""

    state = project_store.get(project_id)
    candidate = next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)
    if candidate is None:
        historical_run = next(
            (
                run
                for run in reversed(state.luminaire_search_runs)
                if luminaire_id in run.candidate_ids
            ),
            None,
        )
        return {
            "status": "candidate_refresh_required",
            "luminaire_id": luminaire_id,
            "message": (
                "该灯具 ID 来自未保存的历史搜索结果，不能直接读取。"
                "请先调用 get_project 获取最新 revision，再调用 search_luminaires 重新保存候选。"
            ),
            "saved_candidate_ids": [item.luminaire_id for item in state.luminaires],
            "historical_search_run_id": (
                historical_run.search_run_id if historical_run is not None else None
            ),
        }
    return {
        "status": "ok",
        "candidate": candidate_summary(candidate),
        "detail_fields": candidate.detail_fields,
        "untrusted_supplier_data": True,
    }


@tool("send_luminaire_to_dialux", args_schema=SendToDialuxInput)
def send_luminaire_to_dialux(project_id: str, luminaire_id: str) -> dict:
    """Hand one saved candidate to the local DIALux evo via the dial:// protocol (Windows)."""

    state = project_store.get(project_id)
    candidate = next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)
    if candidate is None:
        return {
            "status": "not_found",
            "luminaire_id": luminaire_id,
            "message": "该灯具不在本项目的已保存候选中，请先调用 search_luminaires。",
        }
    try:
        dial_url = DialuxAPI().resolve_send_to_dialux_url(candidate.detail_url)
    except DialuxAPIError as error:
        return {"status": "vendor_error", "vendor_error": error.as_dict()}
    try:
        handler = dialux_protocol.open_in_dialux(dial_url)
    except DialuxProtocolError as error:
        return {"status": "local_handoff_failed", "error": error.as_dict()}
    return {
        "status": "launched",
        "luminaire_id": luminaire_id,
        "dialux_protocol_url": dial_url,
        "handler": handler,
        "notice": "已通过 dial:// 协议唤起本机 DIALux（与网页“送到 DIALux”按钮等价），请在 DIALux 中确认导入；仿真结论仍需在 DIALux evo 中核验。",
    }


@tool("select_luminaires", args_schema=LuminaireSelectionInput)
def select_luminaires(
    project_id: str,
    expected_revision: int,
    luminaire_ids: list[str],
    group_assignments: dict[str, list[str]] | None = None,
) -> dict:
    """Confirm final project luminaires for DIALux task-package photometry downloads.

    A room usually combines several luminaire types (base lighting, accent or
    emergency lighting), so the list may hold multiple final selections.
    """

    updated = project_store.set_selected_luminaires(
        project_id, expected_revision, luminaire_ids, group_assignments
    )
    return {
        "selected_luminaire_ids": updated.selected_luminaire_ids,
        "luminaire_group_assignments": updated.luminaire_group_assignments,
        "project_revision": updated.revision,
        "rebased": False,
    }


@tool("ask_user", args_schema=AskUserInput)
def ask_user(title: str, question: str, fields: list[ClarificationField]) -> dict:
    """Request missing user input as a structured, fillable form and pause the workflow."""

    return {
        "status": "awaiting_user_input",
        "title": title,
        "question": question,
        "fields": [_data(field) for field in fields],
    }


@tool("create_dialux_task_package", args_schema=DialuxTaskInput)
def create_dialux_task_package(project_id: str, expected_revision: int) -> dict:
    """Create a ZIP handoff with the task manifest and named photometry ZIP files."""

    state = project_store.get(project_id)
    if state.revision != expected_revision:
        raise RevisionConflictError(
            f"Project revision is {state.revision}, but request expected {expected_revision}"
        )
    target = _artifact_path(project_id, ".dialux-task.zip")
    target.write_bytes(build_dialux_task_archive(state, PhotometryAssetStore(_project_directory(project_id), DialuxAPI())))
    return {
        "task_package": str(target),
        "handoff": build_dialux_task_package(state),
        "project_revision": state.revision,
        "rebased": False,
    }


@tool("generate_design_report", args_schema=ReportInput)
def generate_design_report(project_id: str, expected_revision: int) -> dict:
    """Generate a Markdown report that contains only saved facts, evidence and explicit limitations."""

    state = project_store.get(project_id)
    target = _artifact_path(project_id, ".design-report.md")
    target.write_text(build_design_report(state), encoding="utf-8")
    return {
        "report": str(target),
        "project_revision": state.revision,
        "rebased": state.revision != expected_revision,
    }


@tool("analyze_luminaire_layout", args_schema=LuminaireLayoutAnalysisInput)
def analyze_luminaire_layout(
    project_id: str,
    expected_revision: int,
    report_file: str,
    coordinate_tolerance_m: float = 0.05,
) -> dict:
    """Match a project's imported DXF luminaire symbols to a PDF report."""

    state = project_store.get(project_id)
    if state.floor_plan is None:
        return {"status": "needs_floor_plan", "message": "请先导入项目 DXF/DWG 平面图。"}
    report_path = Path(report_file).expanduser()
    if not report_path.is_absolute():
        report_path = _project_directory(project_id) / report_path
    report_path = report_path.resolve()
    try:
        report_path.relative_to(_project_directory(project_id).resolve())
    except ValueError as error:
        raise ValueError("report_file must be inside the current project workspace") from error
    try:
        report = parse_luminaire_report(report_path)
    except LuminaireReportParseError as error:
        raise ValueError(str(error)) from error
    analysis = run_luminaire_layout_analysis(
        state.floor_plan,
        report,
        coordinate_tolerance_m=coordinate_tolerance_m,
    )
    updated = project_store.set_layout_analysis(project_id, expected_revision, analysis)
    return {
        "status": "ok",
        "analysis": _data(updated.layout_analysis),
        "project_revision": updated.revision,
        "matched_count": sum(1 for item in analysis.placements if item.matching_status == "matched"),
        "issue_count": len(analysis.issues),
        "notice": "布局分类仍为未分类；照度和 UGR 不能由坐标一致性审查替代。",
    }


# Compatibility aliases used by the original proposal and examples.
rag_search = search_evidence
dialux_search_lights = search_luminaires
