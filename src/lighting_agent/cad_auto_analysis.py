"""Deterministic application of a multimodal CAD interpretation.

The model may select and describe existing CAD candidates, but it cannot create
geometry.  Every accepted decision is checked against the parsed drawing and
is persisted as provenance on the spatial model.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import FieldProvenance, FloorPlan, ModelDecision, SpatialModel
from .spatial_model import assess_model


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        raw = "".join(str(item.get("text", "")) for item in raw if isinstance(item, dict))
    text = str(raw).strip().removeprefix("```json").removesuffix("```").strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型分析结果必须是 JSON 对象")
    return value


def apply_model_analysis(plan: FloorPlan, raw: Any, *, model_name: str | None = None) -> SpatialModel:
    """Apply strict model decisions and return an automatically reviewed copy."""
    result = _json_object(raw)
    model = SpatialModel.model_validate(plan.spatial_model.model_dump() if plan.spatial_model else {})
    room_ids = {room.room_id for room in model.rooms}
    element_ids = {element.element_id for element in model.elements}
    decisions: list[ModelDecision] = []
    problems: list[str] = []

    rooms = result.get("rooms", result.get("spaces", []))
    if not isinstance(rooms, list):
        raise ValueError("模型 rooms 必须是数组")
    decided_rooms: set[str] = set()
    for item in rooms:
        if not isinstance(item, dict):
            problems.append("模型空间项不是对象")
            continue
        target = str(item.get("candidate_id", item.get("room_id", "")))
        if target not in room_ids:
            problems.append(f"模型引用了不存在的空间 {target}")
            continue
        if target in decided_rooms:
            problems.append(f"模型重复判断了空间 {target}")
            continue
        decided_rooms.add(target)
        action = item.get("action", "include" if item.get("include") is True else "exclude")
        if action not in {"include", "exclude"}:
            problems.append(f"空间 {target} 的 action 无效")
            continue
        confidence = float(item.get("confidence", 0))
        evidence = [str(value) for value in item.get("evidence", []) if value]
        decision = ModelDecision(target_id=target, target_type="room", action=action,
                                 confidence=confidence, evidence=evidence,
                                 rationale=str(item.get("rationale", "")))
        decisions.append(decision)
        room = next(room for room in model.rooms if room.room_id == target)
        if confidence < 0.75 or not evidence:
            decision.status = "needs_attention"
            problems.append(f"空间 {target} 的置信度或证据不足")
            continue
        room.status = "confirmed" if action == "include" else "excluded"
        room.exclusion_reason = "模型判定为非照明空间" if action == "exclude" else ""
        room.provenance["automation"] = FieldProvenance(
            source="inferred", locator="; ".join(evidence), confidence=confidence,
            confirmed=True, note=decision.rationale,
        )

        for field in ("floor", "number", "name", "usage"):
            if item.get(field) not in (None, ""):
                value = str(item[field])
                setattr(room, field, value)
                decisions.append(ModelDecision(target_id=target, target_type="room", action="set",
                                               field=field, value=value, confidence=confidence,
                                               evidence=evidence, rationale="模型从图纸文字/上下文推断"))

    for room in model.rooms:
        if room.room_id not in decided_rooms:
            problems.append(f"模型没有判断空间 {room.room_id}")

    if model.rooms and not any(room.status == "confirmed" for room in model.rooms):
        problems.append("模型没有纳入任何照明空间")

    elements = result.get("elements", [])
    if not isinstance(elements, list):
        problems.append("模型 elements 必须是数组")
        elements = []
    decided_elements: set[str] = set()
    for item in elements:
        if not isinstance(item, dict):
            continue
        target = str(item.get("element_id", ""))
        if target not in element_ids:
            problems.append(f"模型引用了不存在的构件 {target}")
            continue
        if target in decided_elements:
            problems.append(f"模型重复判断了构件 {target}")
            continue
        decided_elements.add(target)
        confidence = float(item.get("confidence", 0))
        evidence = [str(value) for value in item.get("evidence", []) if value]
        element = next(element for element in model.elements if element.element_id == target)
        action = item.get("action", "include")
        decision = ModelDecision(target_id=target, target_type="element", action=action,
                                 confidence=confidence, evidence=evidence,
                                 rationale=str(item.get("rationale", "")))
        decisions.append(decision)
        if action not in {"include", "exclude"}:
            decision.status = "needs_attention"
            problems.append(f"构件 {target} 的 action 无效")
        elif action == "exclude" and confidence >= 0.75 and evidence:
            element.status = "excluded"
        elif confidence >= 0.75 and evidence:
            element.status = "confirmed"
        else:
            decision.status = "needs_attention"
            problems.append(f"构件 {target} 的置信度或证据不足")

    for element in model.elements:
        if element.element_id not in decided_elements:
            problems.append(f"模型没有判断构件 {element.element_id}")

    model.model_decisions = decisions
    model.analysis_model = model_name
    model.analysis_sha256 = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    model.coverage_confirmed = not problems and any(room.status == "confirmed" for room in model.rooms)
    model.elements_reviewed = not problems
    model.automation_status = "needs_attention"
    model.audit_log.append("多模态模型自动判定空间范围、构件语义和排除项")
    assess_model(model, plan)
    if problems:
        model.outstanding = list(dict.fromkeys([*problems, *model.outstanding]))
        model.design_ready = False
    if not problems and plan.read_complete and model.meters_per_unit and model.coverage_confirmed:
        model.automation_status = "auto_confirmed"
    return model
