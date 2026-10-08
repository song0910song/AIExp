"""Current project facts; legacy fields remain readable as opaque data."""

from __future__ import annotations

from datetime import UTC, date, datetime
import math
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CompatibleModel(BaseModel):
    # Older projects may contain calculation/simulation data; do not erase it on save.
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class DesignBrief(CompatibleModel):
    project_name: str = Field(min_length=1, max_length=160)
    space_type: str | None = None
    area_m2: float | None = None
    length_m: float | None = None
    width_m: float | None = None
    confirmed_fields: set[str] = Field(default_factory=set)
    cad_confirmed_fields: set[str] = Field(default_factory=set)


class Evidence(StrictModel):
    evidence_id: str = Field(default_factory=lambda: uuid4().hex)
    source_name: str
    source_type: Literal["standard", "project_document", "user_note"]
    excerpt: str
    locator: str | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    score: float | None = None


class LuminaireSearchRequest(CompatibleModel):
    keyword: str = Field(min_length=1, max_length=160)
    language: str = "zh"
    brand: str | None = None
    brand_id: str | None = None
    preferred_brands: list[str] = Field(default_factory=list)
    target_illuminance_lx: float | None = None
    target_cct_k: int | None = None
    target_cct_tolerance_k: int = 0
    min_cri: int | None = None
    max_ugr: float | None = None
    max_power_w: float | None = None
    min_ip_rating: str | None = None
    max_results: int = Field(default=5, ge=1, le=5)


class LuminaireCriterionCheck(StrictModel):
    field: Literal["brand", "max_power_w", "target_cct_k", "min_cri", "max_ugr", "min_ip_rating", "mounting", "detail"]
    status: Literal["pass", "fail", "unknown"]
    expected: str
    observed: str | None = None
    priority: Literal["required", "preference"] = "required"


class LuminaireBriefValidation(CompatibleModel):
    project_revision: int
    constraints: LuminaireSearchRequest
    matching_status: Literal["matches", "incomplete", "rejected"]
    missing_requested_fields: list[str] = Field(default_factory=list)
    failed_requested_fields: list[str] = Field(default_factory=list)
    criteria_checks: list[LuminaireCriterionCheck] = Field(default_factory=list)
    status: str = "current"
    validated_at: datetime = Field(default_factory=utc_now)


class LuminaireSearchRun(CompatibleModel):
    search_run_id: str = Field(default_factory=lambda: uuid4().hex)
    project_id: str | None = None
    project_revision: int | None = None
    request: LuminaireSearchRequest
    original_keyword: str
    resolved_keyword: str
    fallback_keyword: str | None = None
    endpoint: str
    parameters: dict[str, str] = Field(default_factory=dict)
    brand_resolution: dict[str, str] = Field(default_factory=dict)
    candidate_ids: list[str] = Field(default_factory=list)
    detail_status_by_id: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    parser_version: str = "2.0"
    cache_hits: int = 0
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None


class LuminaireCandidate(CompatibleModel):
    luminaire_id: str
    article_name: str
    brand_name: str | None = None
    summary: str | None = None
    technical_summary: str | None = None
    power_w: float | None = None
    luminous_flux_lm: float | None = None
    ip_rating: str | None = None
    cct_k: int | None = None
    cri: int | None = None
    ugr: float | None = None
    detail_url: str
    image_url: str | None = None
    photometry_image_url: str | None = None
    # The catalogue's validated ``dial://`` handoff link.  It is retained as
    # provenance so a confirmed DIALux run can cache the same official ULD
    # without requiring a user-uploaded IES/LDT/ULD file.
    dialux_protocol_url: str | None = None
    has_uld: bool = False
    has_photometry_download: bool = False
    detail_fields: dict[str, str] = Field(default_factory=dict)
    search_run_id: str | None = None
    detail_status: Literal["not_requested", "fetched", "failed", "parse_failed"] = "not_requested"
    parse_warnings: list[str] = Field(default_factory=list)
    brief_validation: LuminaireBriefValidation | None = None
    matching_status: Literal["matches", "incomplete", "rejected"] = "incomplete"
    missing_requested_fields: list[str] = Field(default_factory=list)
    failed_requested_fields: list[str] = Field(default_factory=list)
    criteria_checks: list[LuminaireCriterionCheck] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=utc_now)


class CadPoint(StrictModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class FieldProvenance(StrictModel):
    source: Literal["cad", "inferred", "user_assumption", "user_correction"]
    locator: str
    confidence: float = Field(default=1, ge=0, le=1)
    confirmed: bool = False
    note: str = ""


class ModelIssue(StrictModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    source_handle: str | None = None
    room_id: str | None = None
    position: CadPoint | None = None


class ModelDecision(StrictModel):
    """Auditable result of automatic CAD interpretation."""
    decision_id: str = Field(default_factory=lambda: uuid4().hex)
    target_id: str
    target_type: Literal["room", "element", "drawing"]
    action: Literal["include", "exclude", "set"]
    field: str | None = None
    value: Any | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    rationale: str = ""
    status: Literal["accepted", "rejected", "needs_attention"] = "accepted"


class DrawingPath(StrictModel):
    layer: str
    source_handle: str
    points: list[CadPoint]
    closed: bool = False
    elevation_raw: float = 0


class DrawingLabel(StrictModel):
    text: str
    position: CadPoint
    layer: str
    source_handle: str
    elevation_raw: float = 0


class SpatialElement(StrictModel):
    element_id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal["door", "window", "column", "furniture", "obstruction"]
    name: str = ""
    room_id: str | None = None
    footprint: list[CadPoint] = Field(default_factory=list)
    holes: list[list[CadPoint]] = Field(default_factory=list)
    position: CadPoint | None = None
    length_m: float | None = None
    width_m: float | None = None
    elevation_m: float | None = Field(default=None, allow_inf_nan=False)
    height_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    rotation_deg: float | None = Field(default=None, allow_inf_nan=False)
    material: str | None = None
    reflectance: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    status: Literal["pending", "confirmed", "excluded"] = "pending"
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)


class SpatialRoom(StrictModel):
    room_id: str
    candidate_id: str | None = None
    floor: str | None = None
    number: str | None = None
    name: str | None = None
    usage: str | None = None
    boundary: list[CadPoint] = Field(min_length=3)
    holes: list[list[CadPoint]] = Field(default_factory=list)
    area_m2: float | None = None
    elevation_m: float | None = Field(default=None, allow_inf_nan=False)
    height_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ceiling_height_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    wall_reflectance: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    ceiling_reflectance: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    floor_reflectance: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    status: Literal["pending", "confirmed", "excluded"] = "pending"
    exclusion_reason: str = ""
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)


class SpatialModel(StrictModel):
    version: int = 1
    source_sha256: str
    coordinate_system: str = "CAD WCS XY; boundaries in drawing units; elevations in metres"
    meters_per_unit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    geometry_tolerance_m: float | None = None
    rooms: list[SpatialRoom] = Field(default_factory=list)
    elements: list[SpatialElement] = Field(default_factory=list)
    coverage_confirmed: bool = False
    elements_reviewed: bool = False
    design_ready: bool = False
    outstanding: list[str] = Field(default_factory=list)
    audit_log: list[str] = Field(default_factory=list)
    automation_status: Literal["pending", "auto_confirmed", "needs_attention"] = "pending"
    model_decisions: list[ModelDecision] = Field(default_factory=list)
    analysis_model: str | None = None
    analysis_sha256: str | None = None


class EvidenceBlock(StrictModel):
    block_id: str
    text: str
    bbox: tuple[float, float, float, float] | None = None
    kind: Literal["text", "ocr", "table"] = "text"
    source_bbox: tuple[float, float, float, float] | None = None
    coordinate_space: Literal["pdf_points", "image_pixels", "unknown"] = "pdf_points"


class EvidenceTable(StrictModel):
    table_id: str
    cells: list[list[str | None]]
    bbox: tuple[float, float, float, float] | None = None
    cell_bboxes: list[tuple[float, float, float, float] | None] = Field(default_factory=list)


class EvidencePage(StrictModel):
    page_number: int | None = None
    locator: str
    text: str = ""
    width: float | None = None
    height: float | None = None
    blocks: list[EvidenceBlock] = Field(default_factory=list)
    tables: list[EvidenceTable] = Field(default_factory=list)
    ocr_layout: list[EvidenceBlock] = Field(default_factory=list)
    status: Literal["extracted", "ocr_review", "needs_review", "empty"] = "extracted"
    warnings: list[str] = Field(default_factory=list)


class StandardRecord(StrictModel):
    standard_id: str = Field(default_factory=lambda: uuid4().hex)
    source_hash: str
    file_sha256: str
    project_id: str | None = None
    number: str = Field(min_length=1, max_length=160)
    edition: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    effective_date: date
    scope: str = Field(min_length=1, max_length=2000)
    kind: Literal["official", "corporate", "owner"]
    source: str = Field(min_length=1, max_length=2000)
    source_verified: bool = False
    registered_at: datetime = Field(default_factory=utc_now)


class CalculationConditions(StrictModel):
    plane: str | None = None
    workplane_height_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    grid_x_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    grid_y_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    maintenance_factor: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    glare_method: str | None = None
    glare_observers: str | None = None
    additional: str = ""


class DialuxGeometryAssumptions(StrictModel):
    """Explicit defaults used only after the user confirms a DIALux run."""

    wall_thickness_m: float = Field(default=.12, gt=0, le=1, allow_inf_nan=False)
    floor_slab_thickness_m: float = Field(default=.15, gt=0, le=1, allow_inf_nan=False)
    ceiling_slab_thickness_m: float = Field(default=.10, gt=0, le=1, allow_inf_nan=False)
    default_floor: str = Field(default="1F", min_length=1, max_length=80)
    default_usage: str | None = Field(default=None, max_length=160)
    default_room_height_m: float = Field(default=3.0, gt=0, le=100, allow_inf_nan=False)
    default_clear_height_m: float = Field(default=2.8, gt=0, le=100, allow_inf_nan=False)
    wall_reflectance: float = Field(default=.5, ge=0, le=1, allow_inf_nan=False)
    ceiling_reflectance: float = Field(default=.7, ge=0, le=1, allow_inf_nan=False)
    floor_reflectance: float = Field(default=.2, ge=0, le=1, allow_inf_nan=False)
    door_mode: Literal["closed_door", "open_passage"] = "closed_door"
    door_panel_thickness_m: float = Field(default=.04, gt=0, le=.2, allow_inf_nan=False)
    glazing_panel_thickness_m: float = Field(default=.008, gt=0, le=.2, allow_inf_nan=False)
    glazing_visible_transmittance: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    glazing_refractive_index: float | None = Field(default=None, ge=1, le=3, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_heights(self):
        if self.default_clear_height_m > self.default_room_height_m:
            raise ValueError("默认净高不能高于默认层高")
        if self.glazing_visible_transmittance is not None and self.glazing_refractive_index is None:
            raise ValueError("设置玻璃透射率时必须同时提供折射率")
        if self.glazing_refractive_index is not None and self.glazing_visible_transmittance is None:
            raise ValueError("设置玻璃折射率时必须同时提供透射率")
        return self


class DialuxLayoutItem(StrictModel):
    room_id: str = Field(min_length=1, max_length=160)
    name: str | None = Field(default=None, max_length=160)
    position_m: list[float] = Field(min_length=3, max_length=3)
    rotation_deg: list[float] = Field(default_factory=lambda: [0., 0., 0.], min_length=3, max_length=3)
    dimming: float = Field(default=1., ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_vectors(self):
        values = [*self.position_m, *self.rotation_deg]
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
            raise ValueError("灯具位置和方向必须是有限数值")
        return self


class DialuxOptimizationRequest(StrictModel):
    enabled: bool = False
    max_iterations: int = Field(default=1, ge=1, le=8)
    target_illuminance_lx: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    min_uniformity_u0: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    max_power_w: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    objective: Literal["min_power", "min_luminaire_count", "balanced"] = "balanced"

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.enabled and self.max_iterations < 2:
            raise ValueError("布局优化至少需要两次候选计算")
        if self.enabled and self.target_illuminance_lx is None and self.min_uniformity_u0 is None:
            raise ValueError("布局优化至少需要平均照度或均匀度目标")
        return self


class DialuxRunRequest(StrictModel):
    """Project-scoped request; confirmation is deliberately explicit."""

    expected_revision: int = Field(ge=0)
    photometry_path: str | None = Field(default=None, max_length=300)
    luminaire_id: str | None = Field(default=None, max_length=200)
    assumptions: DialuxGeometryAssumptions = Field(default_factory=DialuxGeometryAssumptions)
    layout: list[DialuxLayoutItem] = Field(default_factory=list, max_length=200)
    optimization: DialuxOptimizationRequest = Field(default_factory=DialuxOptimizationRequest)
    confirm_assumptions: bool = False


class DialuxReadiness(CompatibleModel):
    status: Literal["ready", "needs_confirmation", "blocked"]
    complexity: Literal["simple", "complex", "unknown"]
    can_prepare: bool = False
    can_start: bool = False
    issues: list[str] = Field(default_factory=list)
    assumptions: DialuxGeometryAssumptions = Field(default_factory=DialuxGeometryAssumptions)
    rooms: list[dict[str, Any]] = Field(default_factory=list)
    elements: list[dict[str, Any]] = Field(default_factory=list)
    luminaire_id: str | None = None
    luminaire_name: str | None = None
    photometry_path: str | None = None
    photometry_source: Literal["user_upload", "dialux_catalogue", "missing"] = "missing"


class DialuxRunRecord(CompatibleModel):
    run_id: str
    project_id: str
    status: Literal["draft", "prepared", "queued", "running", "completed", "needs_attention", "failed"]
    profile: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    job_directory: str | None = None
    readiness: DialuxReadiness | None = None
    request: DialuxRunRequest | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    source_sha256: str | None = None
    model_analysis_sha256: str | None = None


class DesignRule(StrictModel):
    rule_id: str = Field(default_factory=lambda: uuid4().hex)
    standard_id: str
    locator: str
    page_number: int | None = None
    evidence_text: str
    evidence_key: str
    metric: Literal["illuminance", "uniformity", "ugr", "cri", "cct", "lpd"]
    operator: Literal[">=", "<=", "=", "range"] | None = None
    threshold: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    upper_threshold: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: str | None = None
    applies_to: list[str] = Field(default_factory=list)
    evaluation_scope: str | None = None
    conditions: CalculationConditions = Field(default_factory=CalculationConditions)
    condition_provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    status: Literal["candidate", "confirmed", "rejected"] = "candidate"
    reviewer: str | None = None
    review_note: str = ""
    reviewed_at: datetime | None = None


class RuleSet(StrictModel):
    version: int = 1
    standards: list[StandardRecord] = Field(default_factory=list)
    rules: list[DesignRule] = Field(default_factory=list)
    bound_version: int | None = None
    bound_model_version: int | None = None
    bound_source_sha256: str | None = None
    status: Literal["draft", "bound", "stale"] = "draft"
    bound_by: str | None = None
    bound_at: datetime | None = None
    coverage_confirmed: bool = False
    binding_note: str | None = None
    invalidation_reason: str | None = None


class FloorPlanAsset(StrictModel):
    source_name: str
    source_type: Literal["dxf", "dwg"]
    storage_path: str
    sha256: str
    size_bytes: int
    converted_from_dwg: bool = False
    imported_at: datetime = Field(default_factory=utc_now)


class FloorPlanAreaCandidate(StrictModel):
    entity_type: Literal["LWPOLYLINE", "POLYLINE"]
    layer: str
    raw_area: float
    area_m2: float | None = None
    length_m: float | None = None
    width_m: float | None = None
    points: list[CadPoint]
    candidate_id: str = ""
    boundary: list[CadPoint] = Field(default_factory=list)
    holes: list[list[CadPoint]] = Field(default_factory=list)
    source_handles: list[str] = Field(default_factory=list)
    geometry_sources: list[dict[str, Any]] = Field(default_factory=list)
    inferred_name: str | None = None
    elevation_raw: float = 0


class FloorPlan(CompatibleModel):
    asset: FloorPlanAsset
    drawing_units: str
    meters_per_drawing_unit: float | None = None
    bounds: tuple[CadPoint, CadPoint] | None = None
    entity_counts: dict[str, int] = Field(default_factory=dict)
    text_items: list[str] = Field(default_factory=list)
    room_name: str | None = None
    area_candidates: list[FloorPlanAreaCandidate] = Field(default_factory=list)
    selected_area_candidate_index: int | None = None
    warnings: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    external_references: list[str] = Field(default_factory=list)
    unsupported_entities: dict[str, int] = Field(default_factory=dict)
    read_complete: bool = False
    issues: list[ModelIssue] = Field(default_factory=list)
    conversion_log: list[str] = Field(default_factory=list)
    repairs: list[str] = Field(default_factory=list)
    drawing_paths: list[DrawingPath] = Field(default_factory=list)
    drawing_labels: list[DrawingLabel] = Field(default_factory=list)
    spatial_model: SpatialModel | None = None


class ProjectState(CompatibleModel):
    project_id: str = Field(default_factory=lambda: uuid4().hex)
    revision: int = 0
    brief: DesignBrief
    luminaires: list[LuminaireCandidate] = Field(default_factory=list)
    luminaire_search_runs: list[LuminaireSearchRun] = Field(default_factory=list)
    floor_plan: FloorPlan | None = None
    rule_set: RuleSet | None = None
    invalidated_dependencies: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("luminaires")
    @classmethod
    def unique_luminaires(cls, values: list[LuminaireCandidate]) -> list[LuminaireCandidate]:
        return list({item.luminaire_id: item for item in values}.values())


class ProjectUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    brief: DesignBrief | None = None
    floor_plan: FloorPlan | None = None
    luminaires: list[LuminaireCandidate] | None = None
    luminaire_search_runs: list[LuminaireSearchRun] | None = None
    rule_set: RuleSet | None = None
    invalidated_dependencies: list[str] | None = None
