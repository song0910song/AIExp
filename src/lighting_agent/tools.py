"""Agent tools backed by auditable services, not ad-hoc chat state."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from langchain.tools import tool
from pydantic import Field

from .calculations import calculate_lumen_method, check_design_rules as run_rule_checks
from .config import PROJECTS_DIRECTORY
from .dialux_api import (
    DialuxAPI,
    DialuxAPIError,
    candidate_summary,
    validate_luminaire_search,
)
from .deliverables import build_design_report, build_dialux_task_archive, build_dialux_task_package
from .document_loader import load_document
from .project_store import ProjectStore, RevisionConflictError
from .photometry_assets import PhotometryAssetStore
from .rag import create_evidence_store, format_evidence
from .schemas import (
    CalculationInput,
    DesignBrief,
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


class BriefUpdateInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    brief: DesignBrief


class RagLightingParameterInput(ProjectReference):
    """Evidence-backed values extracted from approved RAG results."""

    expected_revision: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    target_illuminance_lx: float | None = Field(default=None, gt=0, le=100_000)
    target_cct_k: int | None = Field(default=None, ge=1_000, le=20_000)
    min_cri: int | None = Field(default=None, ge=0, le=100)
    target_ugr: float | None = Field(default=None, ge=0, le=40)
    target_uniformity_u0: float | None = Field(default=None, ge=0, le=1)
    max_lpd_w_m2: float | None = Field(default=None, gt=0, le=1_000)


class ProjectCalculationInput(ProjectReference):
    expected_revision: int = Field(ge=0)
    inputs: CalculationInput


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


class LuminaireDetailInput(ProjectReference):
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


project_store = ProjectStore()
evidence_store = create_evidence_store()


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


@tool("apply_rag_lighting_parameters", args_schema=RagLightingParameterInput)
def apply_rag_lighting_parameters(
    project_id: str,
    expected_revision: int,
    evidence_ids: list[str],
    target_illuminance_lx: float | None = None,
    target_cct_k: int | None = None,
    min_cri: int | None = None,
    target_ugr: float | None = None,
    target_uniformity_u0: float | None = None,
    max_lpd_w_m2: float | None = None,
) -> dict:
    """Persist RAG-derived lighting targets with field-level evidence provenance.

    Only use values explicitly supported by the retrieved evidence and
    applicable to the current room/use. Existing manually or document-set
    values are never overwritten by this tool.
    """

    updates = {
        name: value
        for name, value in {
            "target_illuminance_lx": target_illuminance_lx,
            "target_cct_k": target_cct_k,
            "min_cri": min_cri,
            "target_ugr": target_ugr,
            "target_uniformity_u0": target_uniformity_u0,
            "max_lpd_w_m2": max_lpd_w_m2,
        }.items()
        if value is not None
    }
    if not updates:
        raise ValueError("At least one RAG-derived lighting parameter is required")

    evidence_ids = list(dict.fromkeys(evidence_ids))
    evidence = evidence_store.get_evidence(evidence_ids)
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
            if current_value is None or current_source is not None:
                applicable_updates[name] = value
            elif current_value != value:
                protected_fields.append(name)
        if protected_fields:
            raise ValueError(
                "RAG cannot overwrite manually or document-confirmed parameters: "
                + ", ".join(protected_fields)
            )
        if not applicable_updates:
            raise ValueError("No missing or RAG-derived lighting parameters can be updated")
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


@tool("search_evidence")
def search_evidence(query: str) -> dict:
    """Search approved project and standard extracts. Returns source text and locators, never invented rules."""

    evidence = evidence_store.search(query, top_k=3)
    return {"evidence": [_data(item) for item in evidence], "formatted": format_evidence(evidence)}


@tool("adopt_evidence", args_schema=EvidenceAdoptionInput)
def adopt_evidence(project_id: str, expected_revision: int, evidence_ids: list[str]) -> dict:
    """Attach retrieved evidence to a project revision before using it in a formal report."""

    adopted = evidence_store.get_evidence(evidence_ids)
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


@tool("add_document")
def add_document(file_path: str, source_type: str = "project_document") -> dict:
    """Index an approved workspace .pdf, .docx, .md or .txt document for evidence retrieval."""

    if source_type not in {"standard", "project_document", "user_note"}:
        raise ValueError("source_type must be standard, project_document or user_note")
    document = load_document(file_path)
    chunk_count = evidence_store.add_document(document, source_type=source_type)
    return {
        "source_name": document.source_name,
        "sha256": document.sha256,
        "page_count": document.page_count,
        "indexed_chunks": chunk_count,
    }


@tool("calculate_preliminary_lighting", args_schema=ProjectCalculationInput)
def calculate_preliminary_lighting(project_id: str, expected_revision: int, inputs: CalculationInput) -> dict:
    """Run the reproducible lumen-method calculation and save its inputs, outputs and limitations."""

    result = calculate_lumen_method(inputs)
    updated, rebased = _update_at_latest_revision(
        project_id,
        expected_revision,
        lambda current: ProjectUpdate(
            expected_revision=current.revision,
            calculations=[*current.calculations, result],
        ),
    )
    return {"calculation": _data(result), "project_revision": updated.revision, "rebased": rebased}


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
        "notice": "候选灯具需在 DIALux evo 结合空间、反射比、安装高度和布灯方式核验照度、均匀度及 UGR。",
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


@tool("select_luminaires", args_schema=LuminaireSelectionInput)
def select_luminaires(project_id: str, expected_revision: int, luminaire_ids: list[str]) -> dict:
    """Confirm final project luminaires for DIALux task-package photometry downloads.

    A room usually combines several luminaire types (base lighting, accent or
    emergency lighting), so the list may hold multiple final selections.
    """

    updated = project_store.set_selected_luminaires(project_id, expected_revision, luminaire_ids)
    return {
        "selected_luminaire_ids": updated.selected_luminaire_ids,
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
    target = PROJECTS_DIRECTORY / f"{project_id}.dialux-task.zip"
    target.write_bytes(build_dialux_task_archive(state, PhotometryAssetStore(PROJECTS_DIRECTORY, DialuxAPI())))
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
    target = PROJECTS_DIRECTORY / f"{project_id}.design-report.md"
    target.write_text(build_design_report(state), encoding="utf-8")
    return {
        "report": str(target),
        "project_revision": state.revision,
        "rebased": state.revision != expected_revision,
    }


# Compatibility aliases used by the original proposal and examples.
rag_search = search_evidence
dialux_search_lights = search_luminaires
