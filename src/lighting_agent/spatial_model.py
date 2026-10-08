"""Room review and readiness, separate from CAD file-read completeness."""
from __future__ import annotations

import re

from shapely import Point, Polygon

from .schemas import FieldProvenance, FloorPlan, SpatialElement, SpatialModel, SpatialRoom

REQUIRED_ROOM_FIELDS = ("floor", "number", "name", "usage", "elevation_m", "height_m",
                        "ceiling_height_m", "wall_reflectance", "ceiling_reflectance", "floor_reflectance")


def room_polygon(room: SpatialRoom) -> Polygon:
    polygon = Polygon([(p.x, p.y) for p in room.boundary], [[(p.x, p.y) for p in h] for h in room.holes])
    if not polygon.is_valid or polygon.area <= 0:
        raise ValueError(f"房间 {room.room_id} 的边界自交、无效或面积为零")
    return polygon


def element_polygon(element: SpatialElement) -> Polygon:
    return Polygon(
        [(point.x, point.y) for point in element.footprint],
        [[(point.x, point.y) for point in ring] for ring in element.holes],
    )


def build_spatial_model(plan: FloorPlan, elements: list) -> SpatialModel:
    rooms = [SpatialRoom(
        room_id=c.candidate_id, candidate_id=c.candidate_id, name=c.inferred_name,
        boundary=c.boundary, holes=c.holes, area_m2=c.area_m2,
        elevation_m=c.elevation_raw * plan.meters_per_drawing_unit if plan.meters_per_drawing_unit else None,
        provenance={
            "boundary": FieldProvenance(source="inferred" if c.layer == "CONTOUR" else "cad", locator=", ".join(c.source_handles), confidence=0.9),
            "elevation_m": FieldProvenance(source="cad", locator="WCS Z 坐标"),
            **({"name": FieldProvenance(source="inferred", locator="图内文字位置", confidence=0.5)} if c.inferred_name else {}),
        },
    ) for c in plan.area_candidates]
    for element in elements:
        if not element.footprint:
            continue
        footprint = element_polygon(element)
        if not footprint.is_valid or footprint.area <= 0:
            continue
        centre = footprint.representative_point()
        matches = [r for r in rooms if room_polygon(r).covers(centre)]
        if len(matches) > 1 and element.elevation_m is not None:
            matches = [r for r in matches if r.elevation_m is not None and abs(r.elevation_m - element.elevation_m) < .001]
        if len(matches) == 1:
            element.room_id = matches[0].room_id
    model = SpatialModel(source_sha256=plan.asset.sha256, meters_per_unit=plan.meters_per_drawing_unit,
                         geometry_tolerance_m=0.001 if plan.meters_per_drawing_unit else None,
                         rooms=rooms, elements=elements, audit_log=["CAD 导入；等待模型自动判定空间范围和语义"])
    return assess_model(model, plan)


def assess_model(model: SpatialModel, plan: FloorPlan) -> SpatialModel:
    missing = []
    if model.meters_per_unit is None:
        missing.append("图纸尺度待校准")
    if not plan.read_complete:
        read_reasons = []
        if plan.external_references:
            read_reasons.append(f"有 {len(plan.external_references)} 项外部参照未载入")
        if plan.unsupported_entities:
            kinds = "、".join(
                f"{kind} × {count}" for kind, count in sorted(plan.unsupported_entities.items())
            )
            read_reasons.append(f"有未解析实体：{kinds}")
        read_reasons.extend(
            issue.message for issue in plan.issues
            if issue.severity == "error" and issue.code not in {"external_reference", "unsupported_entity"}
        )
        reason = "；".join(dict.fromkeys(read_reasons)) or "仍有未解析内容"
        missing.append(f"CAD 文件读取不完整：{reason}；修复或转换后重新导入")
    if not model.coverage_confirmed:
        missing.append("待逐房间核对遗漏、断口、重叠边界和候选排除范围")
    if not model.elements_reviewed:
        missing.append("门窗、柱、家具及遮挡物的完整性待确认（可确认确实不存在）")
    active = [r for r in model.rooms if r.status != "excluded"]
    if not active:
        missing.append("没有待设计房间")
    ids = [r.room_id for r in model.rooms]
    if len(set(ids)) != len(ids):
        raise ValueError("房间 ID 不可重复")
    element_ids = [e.element_id for e in model.elements]
    if len(set(element_ids)) != len(element_ids):
        raise ValueError("构件 ID 不可重复")
    polygons = {}
    for room in model.rooms:
        polygon = room_polygon(room)
        room.area_m2 = polygon.area * model.meters_per_unit**2 if model.meters_per_unit else None
        if room.status == "excluded":
            if not room.exclusion_reason:
                missing.append(f"{room.room_id}：排除原因待填写")
            continue
        polygons[room.room_id] = polygon
        label = room.name or room.number or room.room_id
        if room.status != "confirmed":
            missing.append(f"{label}：房间边界待确认")
        for field in REQUIRED_ROOM_FIELDS:
            if getattr(room, field) in (None, ""):
                missing.append(f"{label}：{field} 待确认")
        if room.height_m and room.ceiling_height_m and room.ceiling_height_m > room.height_m:
            raise ValueError(f"{label}：吊顶高度不能高于层高")
    for index, a in enumerate(active):
        for b in active[index + 1:]:
            if a.floor == b.floor and polygons[a.room_id].intersection(polygons[b.room_id]).area > 1e-8:
                missing.append(f"房间 {a.name or a.room_id} 与 {b.name or b.room_id} 边界重叠；请修正或排除外轮廓候选")
    for element in model.elements:
        if re.search(r"\bDLX_(?:APERT|OBJ|LUM|CALC)\b", element.name, re.I):
            continue
        if len(element.footprint) >= 3:
            footprint = element_polygon(element)
            if footprint.is_valid and footprint.area > 0:
                element.position = type(element.footprint[0])(x=footprint.centroid.x, y=footprint.centroid.y)
                rectangle = list(footprint.minimum_rotated_rectangle.exterior.coords)
                lengths = [Point(a).distance(Point(b)) for a, b in zip(rectangle, rectangle[1:])]
                element.length_m = max(lengths) * model.meters_per_unit if model.meters_per_unit else None
                element.width_m = min(lengths) * model.meters_per_unit if model.meters_per_unit else None
        if element.status == "excluded":
            continue
        if element.room_id not in polygons:
            missing.append(f"{element.name}：所属房间待确认")
        footprint = element_polygon(element)
        if len(element.footprint) < 3 or not footprint.is_valid or footprint.area <= 0:
            missing.append(f"{element.name}：构件占地边界待确认")
        for field in ("height_m", "elevation_m", "rotation_deg", "material", "reflectance"):
            if getattr(element, field) in (None, ""):
                missing.append(f"{element.name}：{field} 待确认")
        if element.status != "confirmed":
            missing.append(f"{element.name}：构件待确认")
    model.outstanding = missing
    model.design_ready = not missing
    return model
