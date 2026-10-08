"""Explicit review helpers for running a real CAD drawing through DIALux.

The CAD parser intentionally keeps uncertain candidates visible.  This module
turns that review into an auditable, immutable model only when the operator
names the room to keep and explicitly acknowledges harmless annotation-only
entities.  It never guesses a room from area or silently discards geometry.
"""
from __future__ import annotations

import re
from pathlib import Path

from lighting_agent.floor_plan import FloorPlanParseError, parse_floor_plan
from lighting_agent.schemas import (
    DialuxGeometryAssumptions,
    FieldProvenance,
    FloorPlan,
    SpatialModel,
)

from .artifacts import RunError
from .generic_jobs import prepare_reviewed_model


# ACAD_TABLE is an annotation/table object in the sample drawing.  It is not
# used as a room, obstacle or opening; all geometric entities are still parsed
# and retained in the source CAD archive.
SAFE_ANNOTATION_ENTITIES = frozenset({"ACAD_TABLE"})
_MEASUREMENT_PATTERN = re.compile(
    r"(?:(?<!\d)\d+(?:[.,]\d+)?\s*(?:m²|㎡|平方米|sq\.?\s*m)|"
    r"\d+(?:[.,]\d+)?\s*W\s*/\s*(?:m²|㎡|m))(?!\w)",
    re.IGNORECASE,
)


def _clean_room_name(value: str | None, fallback: str) -> str:
    value = (value or fallback).strip()
    value = _MEASUREMENT_PATTERN.sub("", value)
    value = re.sub(r"\(\s*\)", "", value).strip()
    return value or fallback


def prepare_real_cad_model(
    cad_path: Path,
    *,
    candidate_index: int = 0,
    room_number: str | None = None,
    room_name: str | None = None,
    usage: str = "Meeting room",
    assumptions: DialuxGeometryAssumptions | None = None,
) -> tuple[SpatialModel, FloorPlan, dict]:
    """Parse and explicitly confirm one real-CAD room for the generic profile.

    Other detected candidates remain in the model as ``excluded`` rooms with a
    reason.  Only known annotation-only entities may be acknowledged here;
    external references and unsupported geometry remain hard blockers.
    """

    try:
        plan = parse_floor_plan(cad_path, storage_path=cad_path.name)
    except FloorPlanParseError as error:
        raise RunError(str(error)) from error
    if plan.spatial_model is None or not plan.area_candidates:
        raise RunError("真实 CAD 未识别到可供人工确认的房间候选")
    if plan.meters_per_drawing_unit is None:
        raise RunError("真实 CAD 缺少可确认的米制尺度")
    if plan.external_references:
        raise RunError("真实 CAD 包含未载入外部参照：" + ", ".join(plan.external_references))
    unsafe = sorted(set(plan.unsupported_entities) - SAFE_ANNOTATION_ENTITIES)
    if unsafe:
        raise RunError("真实 CAD 包含未建模实体，不能安全执行：" + ", ".join(unsafe))
    if any(issue.severity == "error" for issue in plan.issues):
        raise RunError("真实 CAD 存在几何错误，不能安全执行：" + "; ".join(
            issue.message for issue in plan.issues if issue.severity == "error"
        ))
    if not 0 <= candidate_index < len(plan.area_candidates):
        raise RunError(f"房间候选编号超出范围：{candidate_index}")

    candidate = plan.area_candidates[candidate_index]
    model = plan.spatial_model.model_copy(deep=True)
    selected_id = candidate.candidate_id
    selected = next(room for room in model.rooms if room.room_id == selected_id)
    selected.status = "confirmed"
    selected.floor = selected.floor or "1F"
    selected.number = room_number or selected.number or "101"
    selected.name = room_name or _clean_room_name(candidate.inferred_name, selected.number)
    selected.usage = usage
    selected.provenance["boundary"] = FieldProvenance(
        source="user_correction", locator=f"真实 CAD 人工核对候选 {candidate_index}",
        confidence=1, confirmed=True,
        note="保留原始 DXF WCS 边界；其余候选显式排除",
    )
    for room in model.rooms:
        if room.room_id == selected_id:
            continue
        room.status = "excluded"
        room.exclusion_reason = "人工核对：非本次照明设计房间/重复边界，保留候选以便追溯"
        room.provenance["status"] = FieldProvenance(
            source="user_correction", locator=f"真实 CAD 人工核对候选 {candidate_index}",
            confidence=1, confirmed=True, note=room.exclusion_reason,
        )
    for element in model.elements:
        if element.room_id != selected_id:
            element.status = "excluded"
            element.provenance["status"] = FieldProvenance(
                source="user_correction", locator="真实 CAD 人工核对",
                confidence=1, confirmed=True, note="不属于保留房间，显式排除",
            )

    model.coverage_confirmed = True
    model.elements_reviewed = True
    model.outstanding = []
    model.audit_log.extend([
        f"真实 CAD 人工核对：保留候选 {candidate_index} ({selected_id})，其余 {len(model.rooms) - 1} 个候选显式排除",
        "确认 DXF 中所有几何实体已读取；仅 ACAD_TABLE 注释实体不参与 IFC 几何",
    ])
    effective, readiness = prepare_reviewed_model(
        model,
        assumptions or DialuxGeometryAssumptions(default_usage=usage),
        default_usage=usage,
        confirm=True,
    )
    if not readiness.can_prepare:
        raise RunError("真实 CAD 房间确认后仍不能准备 DIALux：" + "; ".join(readiness.issues))
    review = {
        "profile": "real-cad-reviewed-v1",
        "candidate_index": candidate_index,
        "candidate_id": selected_id,
        "candidate_source_handles": candidate.source_handles,
        "excluded_candidate_ids": [room.room_id for room in model.rooms if room.room_id != selected_id],
        "accepted_annotation_entities": dict(plan.unsupported_entities),
        "read_complete_before_review": plan.read_complete,
        "issues_acknowledged": [issue.code for issue in plan.issues],
        "source_sha256": plan.asset.sha256,
    }
    return effective, plan, review
