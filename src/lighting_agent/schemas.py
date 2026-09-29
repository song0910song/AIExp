"""Current project facts; legacy fields remain readable as opaque data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    x: float
    y: float


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


class ProjectState(CompatibleModel):
    project_id: str = Field(default_factory=lambda: uuid4().hex)
    revision: int = 0
    brief: DesignBrief
    luminaires: list[LuminaireCandidate] = Field(default_factory=list)
    luminaire_search_runs: list[LuminaireSearchRun] = Field(default_factory=list)
    floor_plan: FloorPlan | None = None
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
