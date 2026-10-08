"""Preparation and validation for reviewed DXF/DWG DIALux jobs.

Rectangular room sets use the validated shared-wall envelope exporter. A single
room may use an arbitrary valid polygon (including tessellated curves, holes,
and oblique walls) through the IFC profile exporter. Unsupported topology is
reported explicitly instead of being replaced with a bounding rectangle.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

import ifcopenshell
from shapely.geometry import Polygon, Point, box

from lighting_agent.dialux_runner.envelope import OpeningTreatment, export_envelope
from lighting_agent.ifc_export import (
    MAX_IFC_PROFILE_VERTICES, IfcExportError, IfcExportOptions, export_spatial_model,
)
from lighting_agent.schemas import (
    DialuxGeometryAssumptions, DialuxLayoutItem, DialuxOptimizationRequest,
    DialuxReadiness, SpatialModel,
)
from .artifacts import MAX_LIMITS, RunError, atomic_json, sha256, timestamp, verify_artifacts


GENERIC_PROFILE = "evo-5.14.0.3-zh-generic-ifc-v1"
PHOTOMETRY_EXTENSIONS = {".ies", ".ldt", ".uld"}
SETTINGS = {
    "lighting_mode": "artificial_only",
    "daylight": False,
    "workplane_height_m": .8,
    "edge_margin_m": .5,
    "maintenance_factor": .8,
    "grid_mode": "dialux_workplane_adaptive",
    "direct_only": False,
    "exclude_furniture": False,
    "virtual_surfaces_only": False,
    "simplified_furniture": False,
}


def _binding(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _metric_polygon(room, scale: float) -> Polygon:
    return Polygon(
        [(point.x * scale, point.y * scale) for point in room.boundary],
        [[(point.x * scale, point.y * scale) for point in hole] for hole in room.holes],
    )


def _is_axis_aligned_rectangle(polygon: Polygon) -> bool:
    if not polygon.is_valid or polygon.area <= 1e-7 or polygon.interiors:
        return False
    bounds = box(*polygon.bounds)
    return polygon.symmetric_difference(bounds).area <= 1e-7


def _is_simple_orthogonal_polygon(polygon: Polygon) -> bool:
    """Accept a hole-free stepped room without inventing diagonal geometry."""

    if not polygon.is_valid or polygon.area <= 1e-7 or polygon.interiors:
        return False
    coordinates = list(polygon.exterior.coords)
    if len(coordinates) < 5:
        return False
    for first, second in zip(coordinates, coordinates[1:]):
        dx = abs(second[0] - first[0])
        dy = abs(second[1] - first[1])
        if dx <= 1e-7 and dy <= 1e-7:
            return False
        if dx > 1e-7 and dy > 1e-7:
            return False
    return True


def _is_exportable_polygon(polygon: Polygon) -> bool:
    """Whether IFC can preserve this polygon without repairing or simplifying it."""

    if polygon.geom_type != "Polygon" or not polygon.is_valid or polygon.area <= 1e-7:
        return False
    rings = [polygon.exterior, *polygon.interiors]
    return all(3 <= len(ring.coords) - 1 for ring in rings) and \
        sum(len(ring.coords) - 1 for ring in rings) <= MAX_IFC_PROFILE_VERTICES


def _has_safe_room_spacing(polygons: list[Polygon], wall_thickness_m: float) -> bool:
    for index, polygon in enumerate(polygons):
        for other in polygons[index + 1:]:
            gap = polygon.distance(other)
            if polygon.intersection(other).area > 1e-7 or gap < wall_thickness_m - 1e-7:
                return False
            if wall_thickness_m + 1e-7 < gap < 2 * wall_thickness_m - 1e-7:
                return False
    return True


def geometry_mode(model: SpatialModel, wall_thickness_m: float = .12) -> str:
    """Return the IFC representation that has a validated DIALux path."""

    active_rooms = [room for room in model.rooms if room.status != "excluded"]
    if not active_rooms:
        return "unsupported"
    scale = model.meters_per_unit or 1
    polygons = [_metric_polygon(room, scale) for room in active_rooms]
    if (
        len(active_rooms) == 1
        and _is_exportable_polygon(polygons[0])
        and not any(
            element.status != "excluded" and element.kind in {"door", "window"}
            for element in model.elements
        )
    ):
        return ("orthogonal_single_room" if _is_simple_orthogonal_polygon(polygons[0])
                else "complex_single_room")
    if all(_is_axis_aligned_rectangle(polygon) for polygon in polygons):
        return ("rectangular_envelope" if _has_safe_room_spacing(polygons, wall_thickness_m)
                else "unsupported")
    if (not any(element.status != "excluded" and element.kind in {"door", "window"}
                for element in model.elements)
            and len({(room.floor, room.elevation_m, room.height_m) for room in active_rooms}) == 1
            and all(_is_exportable_polygon(polygon) for polygon in polygons)):
        return ("complex_multiroom" if _has_safe_room_spacing(polygons, wall_thickness_m)
                else "unsupported")
    return "unsupported"


def _room_label(room) -> str:
    number = room.number or room.room_id
    return f"{number} - {room.name}" if room.name else str(number)


def _add_assumption(provenance: dict, field: str, note: str) -> None:
    from lighting_agent.schemas import FieldProvenance

    provenance[field] = FieldProvenance(
        source="user_assumption", locator="DIALux run confirmation", confidence=1, confirmed=True, note=note
    )


def prepare_reviewed_model(
    model: SpatialModel,
    assumptions: DialuxGeometryAssumptions,
    *,
    default_usage: str | None = None,
    confirm: bool = True,
) -> tuple[SpatialModel, DialuxReadiness]:
    """Apply only explicit, reviewable defaults to a private model copy."""

    candidate = SpatialModel.model_validate(model.model_dump())
    issues: list[str] = []
    active_rooms = [room for room in candidate.rooms if room.status != "excluded"]
    if not active_rooms:
        issues.append("没有可参与人工照明计算的房间")
    if candidate.meters_per_unit is None or candidate.meters_per_unit <= 0:
        issues.append("图纸尺度无法换算为米")
    if not candidate.rooms:
        issues.append("未识别到房间边界")
    if candidate.outstanding:
        # Preserve the source diagnostics, but do not treat missing values that
        # the confirmation request can fill as permanent blockers below.
        source_issues = [item for item in candidate.outstanding if not any(
            marker in item for marker in ("待确认", "待设计", "待填写", "待逐房间核对", "完整性待确认")
        )]
        issues.extend(source_issues)
    mode = geometry_mode(candidate, assumptions.wall_thickness_m)
    complexity = "complex" if mode in {"complex_single_room", "complex_multiroom", "unsupported"} else "simple"
    level_conflict = len({(room.floor, room.elevation_m, room.height_m) for room in active_rooms}) > 1
    if level_conflict:
        complexity = "complex"
        issues.append("存在多个楼层、标高或层高，当前 IFC 执行 profile 不能安全合并")
    if mode == "unsupported":
        issues.append("房间边界、房间组合或门窗宿主关系超出当前 IFC 几何 profile 的安全范围")
    if not candidate.coverage_confirmed and not confirm:
        issues.append("房间覆盖范围尚未确认")
    if not candidate.elements_reviewed and not confirm:
        issues.append("门窗、家具和遮挡物完整性尚未确认")

    usage = assumptions.default_usage or default_usage
    for room in active_rooms:
        if room.floor in (None, ""):
            room.floor = assumptions.default_floor
            _add_assumption(room.provenance, "floor", "使用运行确认中的默认楼层")
        if room.number in (None, ""):
            room.number = room.room_id
            _add_assumption(room.provenance, "number", "使用空间 ID 作为房间编号")
        if room.name in (None, ""):
            room.name = room.room_id
            _add_assumption(room.provenance, "name", "图纸未提供名称，使用空间 ID")
        if room.usage in (None, ""):
            if usage:
                room.usage = usage
                _add_assumption(room.provenance, "usage", "使用运行确认中的空间用途")
            else:
                issues.append(f"房间 {_room_label(room)} 缺少用途，无法选择合理的工作面")
        if room.elevation_m is None:
            room.elevation_m = 0.
            _add_assumption(room.provenance, "elevation_m", "未提供标高，按 0 m 处理")
        if room.height_m is None:
            room.height_m = assumptions.default_room_height_m
            _add_assumption(room.provenance, "height_m", "使用运行确认中的默认层高")
        if room.ceiling_height_m is None:
            room.ceiling_height_m = assumptions.default_clear_height_m
            _add_assumption(room.provenance, "ceiling_height_m", "使用运行确认中的默认净高")
        if room.wall_reflectance is None:
            room.wall_reflectance = assumptions.wall_reflectance
            _add_assumption(room.provenance, "wall_reflectance", "使用运行确认中的默认墙面反射率")
        if room.ceiling_reflectance is None:
            room.ceiling_reflectance = assumptions.ceiling_reflectance
            _add_assumption(room.provenance, "ceiling_reflectance", "使用运行确认中的默认顶棚反射率")
        if room.floor_reflectance is None:
            room.floor_reflectance = assumptions.floor_reflectance
            _add_assumption(room.provenance, "floor_reflectance", "使用运行确认中的默认地面反射率")
        if room.status == "pending":
            room.status = "confirmed"

    for element in candidate.elements:
        if element.status == "excluded":
            continue
        if element.room_id is None:
            issues.append(f"构件 {element.name or element.element_id} 未能关联到房间")
            continue
        if element.height_m is None:
            element.height_m = 2.1 if element.kind == "door" else 1.2 if element.kind == "window" else .75
            _add_assumption(element.provenance, "height_m", "使用按构件类型推定的默认高度")
        if element.elevation_m is None:
            element.elevation_m = .9 if element.kind == "window" else 0.
            _add_assumption(element.provenance, "elevation_m", "使用按构件类型推定的默认标高")
        if element.rotation_deg is None:
            element.rotation_deg = 0.
            _add_assumption(element.provenance, "rotation_deg", "未提供朝向，按 0 度处理")
        if element.material in (None, ""):
            element.material = "Unspecified opaque finish"
            _add_assumption(element.provenance, "material", "图纸未提供材质，按不透明简化表面处理")
        if element.reflectance is None:
            element.reflectance = .5
            _add_assumption(element.provenance, "reflectance", "图纸未提供反射率，按 50% 处理")
        if element.status == "pending":
            element.status = "confirmed"

    if any(element.status != "excluded" and element.kind == "window" for element in candidate.elements):
        if assumptions.glazing_visible_transmittance is None or assumptions.glazing_refractive_index is None:
            issues.append("存在窗，但尚未确认玻璃厚度、可见光透射率和折射率")

    if candidate.automation_status == "needs_attention":
        issues.append("CAD 模型自动判断存在冲突或低置信度，不能启动 DIALux")
    if level_conflict:
        issues.append("不同楼层或标高的空间不能合并到当前单楼层 IFC profile")
    if any(room.ceiling_height_m > room.height_m for room in active_rooms):
        issues.append("存在净高高于层高的房间")
    supported_geometry = mode != "unsupported" and not level_conflict
    candidate.coverage_confirmed = bool(candidate.coverage_confirmed or (confirm and supported_geometry))
    candidate.elements_reviewed = bool(candidate.elements_reviewed or (confirm and supported_geometry))
    candidate.design_ready = bool(confirm and supported_geometry and not issues)
    candidate.version += 1
    candidate.audit_log.append("DIALux 执行范围检查：" + ("用户确认默认假设" if confirm else "等待用户确认默认假设"))
    candidate.outstanding = [] if candidate.design_ready else list(dict.fromkeys(issues))
    readiness = DialuxReadiness(
        status="ready" if candidate.design_ready else "needs_confirmation" if complexity != "complex" else "blocked",
        complexity=complexity,
        can_prepare=candidate.design_ready,
        can_start=candidate.design_ready,
        issues=list(dict.fromkeys(issues)),
        assumptions=assumptions,
        rooms=[{"room_id": room.room_id, "name": _room_label(room), "area_m2": room.area_m2,
                "height_m": room.height_m, "clear_height_m": room.ceiling_height_m} for room in active_rooms],
        elements=[{"element_id": item.element_id, "kind": item.kind, "name": item.name,
                    "room_id": item.room_id} for item in candidate.elements if item.status != "excluded"],
    )
    return candidate, readiness


def auto_layout(model: SpatialModel, *, offset_below_ceiling_m: float = .3) -> list[DialuxLayoutItem]:
    layout: list[DialuxLayoutItem] = []
    for room in model.rooms:
        if room.status == "excluded":
            continue
        polygon = _metric_polygon(room, model.meters_per_unit or 1)
        point = polygon.representative_point()
        z = (room.elevation_m or 0) + max(.1, (room.ceiling_height_m or 2.8) - offset_below_ceiling_m)
        layout.append(DialuxLayoutItem(
            room_id=room.room_id, name=f"Auto lamp {room.room_id}",
            position_m=[point.x, point.y, z], rotation_deg=[0., 0., 0.],
        ))
    return layout


def build_opening_treatments(model: SpatialModel, assumptions: DialuxGeometryAssumptions) -> list[OpeningTreatment]:
    treatments = []
    for element in model.elements:
        if element.status == "excluded" or element.kind not in {"door", "window"}:
            continue
        if element.kind == "window":
            if assumptions.glazing_visible_transmittance is None or assumptions.glazing_refractive_index is None:
                raise RunError(f"窗 {element.name or element.element_id} 缺少明确玻璃透射率和折射率")
            treatments.append(OpeningTreatment(
                element_id=element.element_id, filling="glazing",
                panel_thickness_m=assumptions.glazing_panel_thickness_m,
                visible_transmittance=assumptions.glazing_visible_transmittance,
                refractive_index=assumptions.glazing_refractive_index,
                assumption_note="User-confirmed glazing optics for artificial-light calculation",
            ))
        else:
            treatments.append(OpeningTreatment(
                element_id=element.element_id, filling=assumptions.door_mode,
                panel_thickness_m=assumptions.door_panel_thickness_m,
                assumption_note="User-confirmed closed opaque door" if assumptions.door_mode == "closed_door"
                else "User-confirmed open passage; no door leaf",
            ))
    return treatments


def _expected_rooms(model: SpatialModel, project_name: str, layout: list[DialuxLayoutItem]) -> list[dict]:
    rooms = []
    for room in model.rooms:
        if room.status == "excluded":
            continue
        label = _room_label(room)
        rooms.append({
            "building": project_name, "floor": room.floor, "room_id": room.room_id,
            "room": label, "scene": "灯光场景 1",
            "surface": f"工作面 ({label})", "surface_index": None,
            "floor_area_m2": room.area_m2, "workplane_height_m": SETTINGS["workplane_height_m"],
            "edge_margin_m": SETTINGS["edge_margin_m"], "maintenance_factor": SETTINGS["maintenance_factor"],
            "mounting_height_m": next((item.position_m[2] - (room.elevation_m or 0)
                                        for item in layout if item.room_id == room.room_id), room.ceiling_height_m - .3),
            "ceiling_reflectance": room.ceiling_reflectance,
            "wall_reflectance": room.wall_reflectance,
            "floor_reflectance": room.floor_reflectance,
            "elevation_m": room.elevation_m, "clear_height_m": room.ceiling_height_m,
        })
    return rooms


def _validate_layout(model: SpatialModel, layout: list[DialuxLayoutItem]) -> None:
    room_ids = {room.room_id for room in model.rooms if room.status != "excluded"}
    by_room = {room_id: [] for room_id in room_ids}
    scale = model.meters_per_unit or 1
    for item in layout:
        if item.room_id not in room_ids:
            raise RunError(f"布局引用了未参与计算的房间: {item.room_id}")
        room = next(room for room in model.rooms if room.room_id == item.room_id)
        polygon = _metric_polygon(room, scale)
        if not polygon.buffer(1e-6).covers(Point(item.position_m[0], item.position_m[1])):
            raise RunError(f"灯具 {item.name or item.room_id} 不在房间边界内")
        min_z = room.elevation_m or 0
        max_z = min_z + (room.ceiling_height_m or room.height_m or 3)
        if not min_z < item.position_m[2] < max_z:
            raise RunError(f"灯具 {item.name or item.room_id} 高度超出房间范围")
        by_room[item.room_id].append(item)
    missing = sorted(room_id for room_id, items in by_room.items() if not items)
    if missing:
        raise RunError("以下房间没有灯具布局: " + ", ".join(missing))


def validate_layout(model: SpatialModel, layout: list[DialuxLayoutItem]) -> None:
    _validate_layout(model, layout)


def generate_layout_candidates(
    model: SpatialModel, base: list[DialuxLayoutItem], optimization: DialuxOptimizationRequest
) -> list[list[DialuxLayoutItem]]:
    """Generate bounded deterministic candidates; every candidate is later recalculated by DIALux."""

    if not optimization.enabled:
        return [list(base)]
    candidates: list[list[DialuxLayoutItem]] = [list(base)]
    seen = {_binding([item.model_dump() for item in base])}
    directions = ((1., 0.), (-1., 0.), (0., 1.), (0., -1.))
    for index in range(1, optimization.max_iterations):
        dx, dy = directions[(index - 1) % len(directions)]
        step = .25 * (1 + (index - 1) // len(directions))
        candidate = [item.model_copy(update={
            "position_m": [item.position_m[0] + dx * step, item.position_m[1] + dy * step, item.position_m[2]],
            "name": f"{item.name or item.room_id} candidate-{index + 1}",
        }) for item in base]
        try:
            _validate_layout(model, candidate)
        except RunError:
            candidate = list(base)
        key = _binding([item.model_dump() for item in candidate])
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return candidates


def prepare_generic_job(
    *,
    cad_path: Path,
    photometry_path: Path,
    model: SpatialModel,
    assumptions: DialuxGeometryAssumptions,
    layout: list[DialuxLayoutItem],
    optimization: DialuxOptimizationRequest,
    project_name: str,
    project_id: str,
    executable: Path,
    output: Path,
    cad_review: dict | None = None,
    luminaire_id: str | None = None,
    photometry_source: str = "user_upload",
) -> Path:
    """Create an immutable generic job package without opening DIALux."""

    cad_path, photometry_path, executable, output = (
        cad_path.resolve(), photometry_path.resolve(), executable.resolve(), output.resolve()
    )
    if not cad_path.is_file() or cad_path.suffix.casefold() not in {".dxf", ".dwg"}:
        raise RunError("项目 CAD 文件不可用或不是 DXF/DWG")
    if not photometry_path.is_file() or photometry_path.suffix.casefold() not in PHOTOMETRY_EXTENSIONS:
        raise RunError("需要本地 IES、LDT 或 ULD 光度文件才能执行真实计算")
    if photometry_path.stat().st_size == 0:
        raise RunError("光度文件为空")
    if not executable.is_file():
        raise RunError(f"DIALux executable is missing: {executable}")
    _validate_layout(model, layout)
    treatments = build_opening_treatments(model, assumptions)
    options = IfcExportOptions(
        assumptions.wall_thickness_m, assumptions.floor_slab_thickness_m,
        assumptions.ceiling_slab_thickness_m, project_name, project_id,
    )
    mode = geometry_mode(model, options.wall_thickness_m)
    if mode == "unsupported":
        raise RunError("已确认空间模型超出通用 DXF/DWG profile 的安全几何范围")
    try:
        if mode in {"orthogonal_single_room", "complex_single_room"}:
            # The IFC profile retains the exact reviewed boundary, including
            # holes and tessellated curve/diagonal segments. Door/window host
            # relationships remain outside this single-room profile.
            exported = export_spatial_model(model, options)
        else:
            exported = export_envelope(model, options, treatments)
    except IfcExportError as error:
        raise RunError(f"无法把已确认空间模型安全转换为 IFC: {error}") from error

    if output.exists():
        unexpected = [path for path in output.iterdir() if path.name != "record.json"]
        if unexpected:
            raise RunError("Generic run directory already contains execution artifacts")
    else:
        output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    cad_name = "source" + cad_path.suffix.casefold()
    photometry_name = "photometry" + photometry_path.suffix.casefold()
    files = {
        cad_name: cad_path,
        photometry_name: photometry_path,
    }
    (inputs / "spatial-model.json").write_text(model.model_dump_json(indent=2), encoding="utf-8")
    if mode in {"orthogonal_single_room", "complex_single_room"}:
        ifc_data = exported.data
    else:
        ifc_data = exported.ifc.data
    (inputs / "bridge.ifc").write_bytes(ifc_data)
    hashes = {}
    for name, source in files.items():
        shutil.copyfile(source, inputs / name)
        hashes[f"inputs/{name}"] = sha256(inputs / name)
    for name in ("spatial-model.json", "bridge.ifc"):
        hashes[f"inputs/{name}"] = sha256(inputs / name)
    expected = _expected_rooms(model, project_name, layout)
    document = ifcopenshell.file.from_string(ifc_data.decode("utf-8"))
    entity_counts = {
        "IfcSpace": len(document.by_type("IfcSpace")),
        "IfcFurniture": len(document.by_type("IfcFurniture")),
        "IfcDoor": len(document.by_type("IfcDoor")),
        "IfcWindow": len(document.by_type("IfcWindow")),
        "IfcColumn": len(document.by_type("IfcColumn")),
        "IfcBuildingElementProxy": len(document.by_type("IfcBuildingElementProxy")),
    }
    job = {
        "schema_version": 1, "profile": GENERIC_PROFILE, "run_id": uuid4().hex,
        "prepared_at": timestamp(), "purpose": "user-confirmed DXF/DWG artificial-light execution",
        "dialux_executable": str(executable), "dialux_version": "5.14.0.3",
        "inputs": hashes, "ifc_input": "inputs/bridge.ifc", "cad_input": f"inputs/{cad_name}",
        "photometry_input": f"inputs/{photometry_name}", "layout": [item.model_dump() for item in layout],
        "rooms": expected, "expected": expected[0], "settings": SETTINGS, "optimization": optimization.model_dump(),
        "opening_treatments": [item.model_dump() for item in treatments],
        "export_options": asdict(options), "entity_counts": entity_counts,
        "geometry_mode": mode,
        "bindings": {
            "layout": _binding([item.model_dump() for item in layout]),
            "settings": _binding(SETTINGS),
            "optimization": _binding(optimization.model_dump()),
            "opening_treatments": _binding([item.model_dump() for item in treatments]),
            "export_options": _binding(asdict(options)),
            "rooms": _binding(expected),
        },
        "source_cad_sha256": model.source_sha256, "model_version": model.version,
        "luminaire_id": luminaire_id, "photometry_source": photometry_source,
        "limits": dict(MAX_LIMITS),
        "versions": {"model": model.version, "layout": "user-confirmed-layout-v1",
                      "settings": "artificial-only-v1", "rules": None},
    }
    if cad_review is not None:
        job["cad_review"] = cad_review
    atomic_json(output / "job.json", job)
    verify_artifacts(output, hashes)
    return output / "job.json"


def validate_generic_job(store) -> None:
    job = store.job
    if job.get("dialux_version") != "5.14.0.3" or job.get("profile") != GENERIC_PROFILE:
        raise RunError("Generic DIALux profile version is unsupported")
    if job.get("settings", {}).get("daylight") is not False or job.get("settings", {}).get("lighting_mode") != "artificial_only":
        raise RunError("Generic profile must use artificial light only")
    required = {"inputs/bridge.ifc", "inputs/spatial-model.json", job.get("cad_input"), job.get("photometry_input")}
    if None in required or set(job.get("inputs", {})) != required:
        raise RunError("Generic input package is incomplete")
    model = SpatialModel.model_validate_json((store.root / "inputs/spatial-model.json").read_text(encoding="utf-8"))
    export_options = job.get("export_options", {})
    if job.get("geometry_mode") != geometry_mode(model, export_options.get("wall_thickness_m", .12)):
        raise RunError("Generic geometry profile binding changed")
    cad_review = job.get("cad_review")
    if cad_review is not None:
        active_rooms = [room for room in model.rooms if room.status != "excluded"]
        if (
            len(active_rooms) != 1
            or cad_review.get("candidate_id") != active_rooms[0].room_id
            or cad_review.get("source_sha256") != model.source_sha256
            or not isinstance(cad_review.get("candidate_source_handles"), list)
        ):
            raise RunError("Real CAD review binding changed")
    if model.source_sha256 != job.get("source_cad_sha256") or sha256(store.root / job["cad_input"]) != model.source_sha256:
        raise RunError("CAD hash does not match the confirmed spatial model")
    if Path(job["photometry_input"]).suffix.casefold() not in PHOTOMETRY_EXTENSIONS:
        raise RunError("Unsupported photometry extension")
    document = ifcopenshell.open(store.root / "inputs/bridge.ifc")
    if document.schema != "IFC4" or len(document.by_type("IfcSpace")) != len(job.get("rooms", [])):
        raise RunError("IFC does not match the room manifest")
    if not job.get("layout") or not job.get("rooms"):
        raise RunError("Generic job requires rooms and a non-empty layout")
    layout = [DialuxLayoutItem.model_validate(item) for item in job["layout"]]
    _validate_layout(model, layout)
    bindings = job.get("bindings", {})
    if bindings.get("layout") != _binding(job["layout"]):
        raise RunError("Generic layout binding changed")
    if bindings.get("settings") != _binding(job["settings"]):
        raise RunError("Generic calculation settings binding changed")
    if bindings.get("optimization") != _binding(job.get("optimization", {})):
        raise RunError("Generic optimization binding changed")
    if bindings.get("opening_treatments") != _binding(job.get("opening_treatments", [])):
        raise RunError("Generic opening binding changed")
    if bindings.get("export_options") != _binding(job.get("export_options", {})):
        raise RunError("Generic IFC export binding changed")
    if bindings.get("rooms") != _binding(job["rooms"]):
        raise RunError("Generic room binding changed")
    if job.get("optimization", {}).get("enabled") and job["optimization"].get("max_iterations", 0) < 2:
        raise RunError("Optimization requires at least two iterations")
    if job.get("versions") != {"model": model.version, "layout": "user-confirmed-layout-v1",
                                "settings": "artificial-only-v1", "rules": None}:
        raise RunError("Generic version bindings changed")
    if job.get("source_cad_sha256") != model.source_sha256:
        raise RunError("Generic CAD provenance changed")
