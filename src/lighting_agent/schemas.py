"""Versioned, serialisable domain models.

Facts used for calculations live here rather than in a chat history.  All
numeric fields use SI units documented in their field names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


LIGHTING_PARAMETER_FIELDS = frozenset(
    {
        "target_illuminance_lx",
        "target_cct_k",
        "min_cri",
        "target_ugr",
        "target_uniformity_u0",
        "max_lpd_w_m2",
    }
)


class LightingParameterSource(StrictModel):
    """Evidence provenance for a lighting parameter populated from RAG."""

    source: Literal["rag"] = "rag"
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    applied_at: datetime = Field(default_factory=utc_now)

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value for value in values if value))
        if not normalized:
            raise ValueError("evidence_ids must include at least one value")
        return normalized


class DesignBrief(StrictModel):
    """Confirmed input for an indoor lighting design task.

    The assistant may fill lighting targets from applicable RAG evidence. Each
    such value retains source evidence in ``lighting_parameter_sources``.
    """

    project_name: str = Field(min_length=1, max_length=160)
    space_type: str | None = Field(default=None, max_length=100)
    area_m2: float | None = Field(default=None, gt=0, le=100_000)
    length_m: float | None = Field(default=None, gt=0, le=1_000)
    width_m: float | None = Field(default=None, gt=0, le=1_000)
    room_height_m: float | None = Field(default=None, gt=0, le=100)
    mounting_height_m: float | None = Field(default=None, gt=0, le=100)
    workplane_height_m: float | None = Field(default=0.75, ge=0, le=10)
    target_illuminance_lx: float | None = Field(default=None, gt=0, le=100_000)
    target_cct_k: int | None = Field(default=None, ge=1_000, le=20_000)
    min_cri: int | None = Field(default=None, ge=0, le=100)
    target_ugr: float | None = Field(default=None, ge=0, le=40)
    target_uniformity_u0: float | None = Field(default=None, ge=0, le=1)
    max_lpd_w_m2: float | None = Field(default=None, gt=0, le=1_000)
    max_power_w: float | None = Field(default=None, gt=0, le=100_000)
    mounting: str | None = Field(default=None, max_length=100)
    min_ip_rating: str | None = Field(default=None, pattern=r"^IP\d{2}[A-Za-z]?$", max_length=5)
    preferred_brands: list[str] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=4_000)
    confirmed_fields: set[str] = Field(default_factory=set)
    lighting_parameter_sources: dict[str, LightingParameterSource] = Field(default_factory=dict)

    @field_validator("preferred_brands")
    @classmethod
    def unique_brands(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @field_validator("lighting_parameter_sources")
    @classmethod
    def lighting_parameter_sources_only_cover_lighting_fields(
        cls, values: dict[str, LightingParameterSource]
    ) -> dict[str, LightingParameterSource]:
        unknown_fields = sorted(set(values) - LIGHTING_PARAMETER_FIELDS)
        if unknown_fields:
            raise ValueError(
                "lighting_parameter_sources contains non-lighting fields: "
                + ", ".join(unknown_fields)
            )
        return values

    def missing_design_inputs(self) -> list[str]:
        required = ("space_type", "area_m2", "target_illuminance_lx", "mounting_height_m")
        return [name for name in required if getattr(self, name) is None]


class Evidence(StrictModel):
    evidence_id: str = Field(default_factory=lambda: uuid4().hex)
    source_name: str = Field(min_length=1)
    source_type: Literal["standard", "project_document", "user_note"]
    excerpt: str = Field(min_length=1)
    locator: str | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    score: float | None = Field(default=None, ge=0, le=1)


class CalculationInput(StrictModel):
    area_m2: float = Field(gt=0)
    target_illuminance_lx: float = Field(gt=0)
    luminaire_luminous_flux_lm: float = Field(gt=0)
    luminaire_power_w: float = Field(gt=0)
    utilization_factor: float = Field(gt=0, le=1)
    maintenance_factor: float = Field(gt=0, le=1)


class CalculationResult(StrictModel):
    method: Literal["lumen_method"] = "lumen_method"
    inputs: CalculationInput
    required_luminous_flux_lm: float
    luminaire_count: int
    installed_power_w: float
    installed_power_density_w_m2: float
    assumptions: list[str]
    limitations: list[str]
    calculated_at: datetime = Field(default_factory=utc_now)


class RuleRequirement(StrictModel):
    """A deterministic rule with explicit provenance; it is not a hard-coded GB rule."""

    metric: Literal["illuminance_lx", "cri", "lpd_w_m2", "ugr", "uniformity_u0"]
    operator: Literal["min", "max"]
    threshold: float = Field(ge=0)
    evidence_id: str | None = None
    description: str | None = None


class RuleCheck(StrictModel):
    metric: str
    status: Literal["pass", "fail", "not_applicable", "insufficient_data"]
    observed: float | None = None
    threshold: float | None = None
    explanation: str
    evidence_id: str | None = None


class SimulationMetrics(StrictModel):
    """Structured metrics imported from a DIALux or equivalent result."""

    maintained_illuminance_lx: float | None = Field(default=None, ge=0)
    minimum_illuminance_lx: float | None = Field(default=None, ge=0)
    uniformity_u0: float | None = Field(default=None, ge=0, le=1)
    ugr: float | None = Field(default=None, ge=0, le=40)
    installed_power_density_w_m2: float | None = Field(default=None, ge=0)


class DialuxHandoff(StrictModel):
    """Immutable identity and input snapshot for one DIALux handoff."""

    handoff_id: str = Field(min_length=8, max_length=128)
    project_id: str = Field(min_length=8, max_length=64)
    project_revision: int = Field(ge=0)
    input_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_luminaire_ids: list[str] = Field(default_factory=list, max_length=100)
    photometry_sha256_by_luminaire: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class SimulationRun(StrictModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal["preview", "精算", "dialux_handoff"] = "preview"
    status: Literal["pending", "running", "succeeded", "failed", "stale", "unverified", "cancelled"] = "pending"
    input_project_revision: int = Field(ge=0)
    solver_version: str | None = None
    artifact_path: str | None = None
    error: str | None = None
    handoff_id: str | None = Field(default=None, min_length=8, max_length=128)
    input_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selected_luminaire_ids: list[str] = Field(default_factory=list, max_length=100)
    photometry_sha256_by_luminaire: dict[str, str] = Field(default_factory=dict)
    source_file: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_kind: Literal["dialux_pdf", "dialux_csv", "dialux_json", "manual_form"] | None = None
    metrics: SimulationMetrics | None = None
    verification_status: Literal["matched", "mismatch", "incomplete", "unverified", "stale"] = "unverified"
    verification_messages: list[str] = Field(default_factory=list, max_length=50)
    parser_version: str | None = None
    stale_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_scene_revision(cls, values: Any) -> Any:
        if isinstance(values, dict):
            values.pop("input_scene_revision", None)
        return values


class LuminaireSearchRequest(StrictModel):
    keyword: str = Field(min_length=1, max_length=160)
    language: str = Field(default="zh", pattern=r"^[A-Za-z-]{2,5}$")
    brand: str | None = Field(default=None, max_length=100)
    brand_id: str | None = Field(default=None, max_length=128)
    preferred_brands: list[str] = Field(default_factory=list, max_length=20)
    target_illuminance_lx: float | None = Field(default=None, gt=0, le=100_000)
    target_cct_k: int | None = Field(default=None, ge=1_000, le=20_000)
    target_cct_tolerance_k: int = Field(default=0, ge=0, le=2_000)
    min_cri: int | None = Field(default=None, ge=0, le=100)
    max_ugr: float | None = Field(default=None, ge=0, le=40)
    max_power_w: float | None = Field(default=None, gt=0, le=100_000)
    min_ip_rating: str | None = Field(default=None, pattern=r"^IP\d{2}[A-Za-z]?$", max_length=5)
    max_results: int = Field(default=5, ge=1, le=5)

    @model_validator(mode="before")
    @classmethod
    def drop_deprecated_mounting(cls, values: Any) -> Any:
        """Drop the removed mounting filter so stored search runs stay loadable."""

        if isinstance(values, dict):
            values.pop("mounting", None)
        return values

    @field_validator("preferred_brands")
    @classmethod
    def unique_preferred_brands(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))


class LuminaireCriterionCheck(StrictModel):
    """Result of checking one requested luminaire attribute."""

    field: Literal[
        "brand", "max_power_w", "target_cct_k", "min_cri", "max_ugr", "min_ip_rating", "mounting", "detail"
    ]
    status: Literal["pass", "fail", "unknown"]
    expected: str
    observed: str | None = None
    priority: Literal["required", "preference"] = "required"


class LuminaireBriefValidation(StrictModel):
    """A re-check of a saved candidate against one immutable project revision."""

    project_revision: int = Field(ge=0)
    constraints: LuminaireSearchRequest
    matching_status: Literal["matches", "incomplete", "rejected"]
    missing_requested_fields: list[str] = Field(default_factory=list)
    failed_requested_fields: list[str] = Field(default_factory=list)
    criteria_checks: list[LuminaireCriterionCheck] = Field(default_factory=list)
    status: Literal["current", "stale"] = "current"
    validated_at: datetime = Field(default_factory=utc_now)


class LuminaireSearchRun(StrictModel):
    """Reproducible provenance for one vendor-directory lookup."""

    search_run_id: str = Field(default_factory=lambda: uuid4().hex)
    project_id: str | None = Field(default=None, min_length=8, max_length=64)
    project_revision: int | None = Field(default=None, ge=0)
    request: LuminaireSearchRequest
    original_keyword: str = Field(min_length=1, max_length=160)
    resolved_keyword: str = Field(min_length=1, max_length=160)
    fallback_keyword: str | None = Field(default=None, max_length=160)
    endpoint: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    brand_resolution: dict[str, str] = Field(default_factory=dict)
    candidate_ids: list[str] = Field(default_factory=list, max_length=100)
    detail_status_by_id: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    parser_version: str = "2.0"
    cache_hits: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class LuminaireCandidate(StrictModel):
    luminaire_id: str
    article_name: str
    brand_name: str | None = None
    summary: str | None = None
    technical_summary: str | None = None
    power_w: float | None = Field(default=None, ge=0)
    luminous_flux_lm: float | None = Field(default=None, ge=0)
    ip_rating: str | None = None
    cct_k: int | None = Field(default=None, ge=0)
    cri: int | None = Field(default=None, ge=0, le=100)
    ugr: float | None = Field(default=None, ge=0, le=40)
    detail_url: str
    image_url: str | None = None
    photometry_image_url: str | None = None
    has_uld: bool = False
    has_photometry_download: bool = False
    detail_fields: dict[str, str] = Field(default_factory=dict)
    search_run_id: str | None = None
    detail_status: Literal["not_requested", "fetched", "failed", "parse_failed"] = "not_requested"
    parse_warnings: list[str] = Field(default_factory=list, max_length=50)
    brief_validation: LuminaireBriefValidation | None = None
    matching_status: Literal["matches", "incomplete", "rejected"] = "incomplete"
    missing_requested_fields: list[str] = Field(default_factory=list)
    failed_requested_fields: list[str] = Field(default_factory=list)
    criteria_checks: list[LuminaireCriterionCheck] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def drop_deprecated_mounting(cls, values: Any) -> Any:
        """Drop removed supplier fields so stored candidates stay loadable."""

        if isinstance(values, dict):
            values.pop("mounting", None)
            values.pop("dialux_protocol_url", None)
        return values


class PhotometryExtractedFile(StrictModel):
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    file_type: Literal["ies", "ldt", "uld"]


class PhotometryAsset(StrictModel):
    luminaire_id: str
    article_name: str
    status: Literal["pending", "downloaded", "failed", "not_available"]
    source_url: str | None = None
    downloaded_at: datetime | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    zip_file: str | None = None
    zip_size_bytes: int | None = Field(default=None, ge=0)
    extracted_files: list[PhotometryExtractedFile] = Field(default_factory=list)
    error: str | None = None


class CadPoint(StrictModel):
    """A 2D drawing coordinate in the CAD file's native coordinate system."""

    x: float
    y: float


class FloorPlanAsset(StrictModel):
    """Immutable provenance for one uploaded CAD drawing."""

    source_name: str = Field(min_length=1, max_length=180)
    source_type: Literal["dxf", "dwg"]
    storage_path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=50 * 1024 * 1024)
    converted_from_dwg: bool = False
    imported_at: datetime = Field(default_factory=utc_now)


class FloorPlanAreaCandidate(StrictModel):
    """One closed polyline that may represent a usable room boundary."""

    entity_type: Literal["LWPOLYLINE", "POLYLINE"]
    layer: str = Field(min_length=1, max_length=255)
    raw_area: float = Field(gt=0)
    area_m2: float | None = Field(default=None, gt=0, le=100_000)
    length_m: float | None = Field(default=None, gt=0, le=1_000)
    width_m: float | None = Field(default=None, gt=0, le=1_000)
    points: list[CadPoint] = Field(min_length=3, max_length=5_000)


class FloorPlan(StrictModel):
    """Parsed drawing facts. They are never used for design until applied."""

    asset: FloorPlanAsset
    drawing_units: str = Field(min_length=1, max_length=32)
    meters_per_drawing_unit: float | None = Field(default=None, gt=0)
    bounds: tuple[CadPoint, CadPoint] | None = None
    entity_counts: dict[str, int] = Field(default_factory=dict)
    text_items: list[str] = Field(default_factory=list, max_length=200)
    room_name: str | None = Field(default=None, max_length=160)
    area_candidates: list[FloorPlanAreaCandidate] = Field(default_factory=list, max_length=50)
    selected_area_candidate_index: int | None = Field(default=None, ge=0, le=49)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class ProjectState(StrictModel):
    project_id: str = Field(default_factory=lambda: uuid4().hex)
    revision: int = Field(default=0, ge=0)
    brief: DesignBrief
    evidence: list[Evidence] = Field(default_factory=list)
    calculations: list[CalculationResult] = Field(default_factory=list)
    rule_checks: list[RuleCheck] = Field(default_factory=list)
    luminaires: list[LuminaireCandidate] = Field(default_factory=list)
    luminaire_search_runs: list[LuminaireSearchRun] = Field(default_factory=list, max_length=500)
    selected_luminaire_ids: list[str] = Field(default_factory=list, max_length=100)
    floor_plan: FloorPlan | None = None
    simulation_runs: list[SimulationRun] = Field(default_factory=list)
    workflow_status: Literal[
        "draft",
        "brief_confirmed",
        "preliminary_calculated",
        "luminaires_selected",
        "simulation_pending",
        "simulation_verified",
        "needs_revision",
        "accepted",
        "delivered",
    ] = "draft"
    open_questions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def refresh_open_questions(self) -> None:
        self.open_questions = self.brief.missing_design_inputs()

    def refresh_workflow_status(self) -> None:
        """Derive the visible workflow state from persisted project facts."""

        latest_run = self.simulation_runs[-1] if self.simulation_runs else None
        if latest_run is not None and latest_run.verification_status == "matched" and latest_run.status == "succeeded":
            self.workflow_status = "simulation_verified"
        elif latest_run is not None and (
            latest_run.verification_status == "mismatch" or latest_run.status == "stale"
        ):
            self.workflow_status = "needs_revision"
        elif latest_run is not None and (
            latest_run.status == "unverified" or latest_run.verification_status in {"incomplete", "unverified"}
        ):
            self.workflow_status = "simulation_pending"
        elif self.selected_luminaire_ids:
            self.workflow_status = "luminaires_selected"
        elif self.calculations:
            self.workflow_status = "preliminary_calculated"
        elif not self.brief.missing_design_inputs():
            self.workflow_status = "brief_confirmed"
        else:
            self.workflow_status = "draft"

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_scene(cls, values: Any) -> Any:
        if isinstance(values, dict):
            values.pop("plan", None)
            values.pop("scene", None)
        return values

    @field_validator("selected_luminaire_ids")
    @classmethod
    def unique_selected_luminaires(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @model_validator(mode="after")
    def selected_luminaires_must_be_saved_candidates(self) -> "ProjectState":
        saved_ids = {item.luminaire_id for item in self.luminaires}
        unknown_ids = [item for item in self.selected_luminaire_ids if item not in saved_ids]
        if unknown_ids:
            raise ValueError(f"Selected luminaires are not saved project candidates: {', '.join(unknown_ids)}")
        return self

    def selected_luminaires(self) -> list[LuminaireCandidate]:
        """Return final project selections in the user-confirmed order."""

        candidates_by_id = {item.luminaire_id: item for item in self.luminaires}
        return [candidates_by_id[item] for item in self.selected_luminaire_ids]


class ProjectUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    brief: DesignBrief | None = None
    evidence: list[Evidence] | None = None
    calculations: list[CalculationResult] | None = None
    rule_checks: list[RuleCheck] | None = None
    luminaires: list[LuminaireCandidate] | None = None
    luminaire_search_runs: list[LuminaireSearchRun] | None = None
    selected_luminaire_ids: list[str] | None = None
    floor_plan: FloorPlan | None = None
    simulation_runs: list[SimulationRun] | None = None
    open_questions: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
