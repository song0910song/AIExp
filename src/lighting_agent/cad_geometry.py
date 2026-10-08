"""Auditable WCS geometry, source overlay and conservative CAD semantics.

Curves retain their source parameters and a metric flattening tolerance. Preview
decimation is NEVER used for model geometry. Unknown entities remain visible as
issues; limits are explicit rather than silently dropping room candidates.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from ezdxf.path import make_path
from ezdxf.math import Matrix44, Vec3
from shapely import LineString, Point, Polygon, STRtree, ops

from .schemas import (CadPoint, DrawingLabel, DrawingPath, FieldProvenance,
                      FloorPlanAreaCandidate, ModelIssue, SpatialElement)

MAX_ENTITIES = 100_000
MAX_VERTICES = 500_000
MAX_DEPTH = 16
CURVE_TOLERANCE_M = 0.001
GAP_TOLERANCE_M = 0.001
GEOMETRY = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}
ANNOTATIONS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "LEADER", "POINT", "HATCH"}
TABLE_TEXT = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION"}
TABLE_GRID = {"LINE", "LWPOLYLINE", "POLYLINE"}
KINDS = {
    "door": r"door|门", "window": r"window|窗",
    "column": r"column|柱", "furniture": r"furn|desk|chair|table|家具|桌|椅|柜",
    "obstruction": r"obstruct|遮挡|设备",
}


def _point(p) -> CadPoint:
    return CadPoint(x=float(p[0]), y=float(p[1]))


def inspect_drawing(document, scale: float | None) -> dict[str, Any]:
    issues: list[ModelIssue] = []
    repairs: list[str] = []
    unsupported: Counter[str] = Counter()
    records: list[tuple[Any, str, str, str]] = []
    visited = 0
    xrefs = [str(block.name) for block in document.blocks
             if bool(int(block.block.dxf.get("flags", 0)) & (4 | 8))]
    for name in xrefs:
        issues.append(ModelIssue(code="external_reference", message=f"外部参照 {name} 未自动载入", severity="error"))

    def expand(entities, chain: str = "", block_name: str = "", depth: int = 0, inherited_layer: str = ""):
        nonlocal visited
        if depth > MAX_DEPTH:
            issues.append(ModelIssue(code="block_depth", message="块嵌套超过安全上限", severity="error", source_handle=chain))
            return
        for index, entity in enumerate(entities):
            visited += 1
            if visited >= MAX_ENTITIES:
                if not any(i.code == "entity_limit" for i in issues):
                    issues.append(ModelIssue(code="entity_limit", message="实体超过 100000；需拆分图纸", severity="error"))
                return
            handle = f"{chain}/{entity.dxf.get('handle') or index}".strip("/")
            if entity.dxftype() == "ACAD_TABLE":
                table_name = str(entity.get_block_name())
                block = document.blocks.get(table_name)
                if not table_name or block is None:
                    unsupported["ACAD_TABLE"] += 1
                    issues.append(ModelIssue(
                        code="unsupported_entity",
                        message="CAD 表格内容块缺失，无法读取表格文字",
                        source_handle=handle,
                        severity="error",
                    ))
                    continue
                try:
                    source_count = len(block)
                    if visited + source_count >= MAX_ENTITIES:
                        issues.append(ModelIssue(code="entity_limit", message="实体超过 100000；需拆分图纸", severity="error"))
                        return
                    # ezdxf's ACAD_TABLE virtual-entity adapter currently only
                    # translates by the insertion point and omits OCS rotation.
                    # Apply the table's own axes so text positions stay WCS-correct.
                    normal_value = (entity.dxf.get("extrusion")
                                    if entity.dxf.is_supported("extrusion") else (0, 0, 1))
                    normal = Vec3(normal_value).normalize()
                    horizontal = Vec3(entity.dxf.get("horizontal_direction", (1, 0, 0)))
                    horizontal = horizontal - normal * horizontal.dot(normal)
                    if horizontal.magnitude <= 1e-9:
                        raise ValueError("表格方向与法向量平行")
                    x_axis = horizontal.normalize()
                    y_axis = normal.cross(x_axis).normalize()
                    transform = Matrix44.ucs(
                        x_axis, y_axis, normal, origin=entity.get_insert_location()
                    )
                    children = []
                    for source_entity in block:
                        child = source_entity.copy()
                        child.transform(transform)
                        children.append(child)
                    if len(children) != source_count:
                        raise ValueError("表格实体数量不一致")
                    table_text_count = 0
                    grid_count = 0
                    for child_index, child in enumerate(children):
                        visited += 1
                        child_kind = child.dxftype()
                        child_handle = f"{handle}/cell/{child.dxf.get('handle') or child_index}"
                        if child_kind in TABLE_TEXT:
                            records.append((child, child_handle, f"ACAD_TABLE:{table_name}", inherited_layer))
                            table_text_count += 1
                        elif child_kind in TABLE_GRID:
                            # Cell borders are table formatting, not room boundaries.
                            grid_count += 1
                        else:
                            unsupported[child_kind] += 1
                            issues.append(ModelIssue(
                                code="unsupported_table_content",
                                message=f"CAD 表格含有无法安全忽略的内容（{child_kind}）",
                                source_handle=child_handle,
                                severity="error",
                            ))
                    if grid_count:
                        issues.append(ModelIssue(
                            code="table_grid_ignored",
                            message=f"已读取 CAD 表格文字 {table_text_count} 项；{grid_count} 条单元格边框仅作表格格式，不参与空间边界识别",
                            source_handle=handle,
                            position=_point(entity.get_insert_location()),
                            severity="info",
                        ))
                    repairs.append(f"读取 CAD 表格 {handle} 的 {table_text_count} 项文字；排除 {grid_count} 条非空间表格边框")
                except Exception as error:
                    unsupported["ACAD_TABLE"] += 1
                    issues.append(ModelIssue(
                        code="table_content_unreadable",
                        message=f"CAD 表格内容无法完整读取：{error}",
                        source_handle=handle,
                        severity="error",
                    ))
                continue
            if entity.dxftype() == "INSERT":
                name = str(entity.dxf.name)
                if name in xrefs:
                    continue
                def skipped(child, reason):
                    unsupported[child.dxftype()] += 1
                    issues.append(ModelIssue(code="block_transform", message=str(reason), source_handle=handle, severity="error"))
                try:
                    inserts = list(entity.multi_insert()) if entity.mcount > 1 else [entity]
                    source_layer = str(entity.dxf.get("layer", "0"))
                    effective_layer = source_layer if source_layer != "0" else inherited_layer
                    for n, insert in enumerate(inserts):
                        expand(insert.virtual_entities(skipped_entity_callback=skipped), f"{handle}[{n}]", name, depth + 1, effective_layer)
                        expand(insert.attribs, f"{handle}[{n}]/attributes", name, depth + 1, effective_layer)
                    repairs.append(f"展开块 {name} ({handle})，保留 WCS 平移/缩放/旋转")
                except Exception as error:
                    issues.append(ModelIssue(code="block_transform", message=f"块 {name}: {error}", source_handle=handle, severity="error"))
                continue
            records.append((entity, handle, block_name, inherited_layer))
    expand(document.modelspace())
    tolerance = CURVE_TOLERANCE_M / scale if scale else 0.001
    paths: list[DrawingPath] = []
    labels: list[DrawingLabel] = []
    sources: dict[str, dict] = {}
    semantic_groups: dict[tuple[str, str], list[DrawingPath]] = {}
    semantic_handles: set[str] = set()
    closed: list[tuple[Polygon, str, list[str], float]] = []
    vertices = 0
    for entity, handle, block_name, inherited_layer in records:
        kind = entity.dxftype()
        source_layer = str(entity.dxf.get("layer", "0"))
        layer = source_layer if source_layer != "0" else inherited_layer or source_layer
        if kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            value = entity.plain_text() if kind == "MTEXT" else str(entity.dxf.text)
            labels.append(DrawingLabel(text=value, position=_point(entity.dxf.insert), layer=layer, source_handle=handle, elevation_raw=float(entity.dxf.insert.z)))
        elif kind == "DIMENSION":
            try:
                value = str(entity.dxf.get("text", ""))
                if "<>" in value or not value.strip():
                    value = value.replace("<>", f"{entity.get_measurement():g}") or f"{entity.get_measurement():g}"
                position = entity.dxf.get("text_midpoint", entity.dxf.defpoint)
                labels.append(DrawingLabel(text=value, position=_point(position), layer=layer,
                                           source_handle=handle, elevation_raw=float(position.z)))
            except (AttributeError, ValueError, TypeError):
                issues.append(ModelIssue(code="dimension_unreadable", message="一处尺寸标注无法读取数值，请对照原图核对", source_handle=handle))
        if kind not in GEOMETRY:
            if kind not in ANNOTATIONS:
                unsupported[kind] += 1
                issues.append(ModelIssue(code="unsupported_entity", message=f"未建模实体 {kind}，图层 {layer}", source_handle=handle))
            elif kind == "HATCH":
                issues.append(ModelIssue(code="hatch_review", message=f"填充边界未作为房间自动识别：{layer}", source_handle=handle))
            continue
        try:
            path = make_path(entity)
            flattened = []
            for p in path.flattening(tolerance):
                vertices += 1
                if vertices > MAX_VERTICES:
                    raise ValueError("几何顶点超过 500000，请拆分图纸")
                if not all(math.isfinite(v) for v in (p.x, p.y, p.z)):
                    raise ValueError("含非有限坐标")
                flattened.append(p)
            if len(flattened) < 2:
                continue
            if max(p.z for p in flattened) - min(p.z for p in flattened) > tolerance:
                issues.append(ModelIssue(code="nonplanar", message="非水平几何投影为 XY，需核对楼层/标高", source_handle=handle, severity="error"))
            coords = [(p.x, p.y) for p in flattened]
            is_closed = path.is_closed or coords[0] == coords[-1]
            if is_closed and coords[0] == coords[-1]:
                coords.pop()
            drawing_path = DrawingPath(layer=layer, source_handle=handle, points=[_point(p) for p in coords], closed=is_closed, elevation_raw=flattened[0].z)
            paths.append(drawing_path)
            analytic: dict[str, Any] = {"type": kind, "handle": handle, "layer": layer, "block": block_name, "elevation_raw": flattened[0].z}
            for key in ("center", "radius", "start_angle", "end_angle", "extrusion", "major_axis", "ratio", "start_param", "end_param"):
                value = entity.dxf.get(key) if entity.dxf.is_supported(key) else None
                if value is not None:
                    analytic[key] = value if isinstance(value, (float, int, str)) else list(value)
            if kind == "LWPOLYLINE":
                analytic["vertices_xyseb"] = [list(p) for p in entity.get_points("xyseb")]
            if kind == "SPLINE":
                analytic.update(degree=entity.dxf.degree, control_points=[list(p) for p in entity.control_points],
                                fit_points=[list(p) for p in entity.fit_points], knots=list(entity.knots), weights=list(entity.weights))
            if kind == "POLYLINE":
                analytic["vertices"] = [{"point": list(v.dxf.location), "bulge": v.dxf.get("bulge", 0)} for v in entity.vertices]
            sources[handle] = analytic
            internal_auxiliary = bool(re.fullmatch(r"DLX_(?:APERT|OBJ|LUM|CALC)(?:_.*)?", layer, re.I))
            semantic = next((k for k, pattern in KINDS.items() if re.search(pattern, f"{layer} {block_name}", re.I)), None)
            if semantic:
                semantic_handles.add(handle)
                group = handle.split("]")[0] + "]" if block_name else handle
                semantic_groups.setdefault((semantic, group), []).append(drawing_path)
            if is_closed and len(coords) >= 3 and not semantic and not internal_auxiliary:
                polygon = Polygon(coords)
                if not polygon.is_valid or polygon.area <= 0:
                    issues.append(ModelIssue(code="invalid_boundary", message="闭合边界自交或退化，请人工修正", source_handle=handle, position=_point(coords[0])))
        except Exception as error:
            unsupported[kind] += 1
            issues.append(ModelIssue(code="geometry_error", message=f"{kind}: {error}", source_handle=handle, severity="error"))
            if vertices > MAX_VERTICES:
                break

    preferred = {p.layer for p in paths if re.search(r"wall|cont|墙|房间|room|structural", p.layer, re.I)}
    contour_paths = [p for p in paths if p.source_handle not in semantic_handles and
                     not re.fullmatch(r"DLX_(?:APERT|OBJ|LUM|CALC)(?:_.*)?", p.layer, re.I) and
                     (not preferred or p.layer in preferred) and
                     not any(re.search(pattern, p.layer, re.I) for pattern in KINDS.values())]
    by_elevation: dict[float, list[DrawingPath]] = {}
    for path in contour_paths:
        by_elevation.setdefault(path.elevation_raw, []).append(path)
    for elevation, level_paths in by_elevation.items():
        closed.extend(_polygonize_level(level_paths, elevation, scale, tolerance, issues, repairs))
    candidates = []
    seen_polygons: set[bytes] = set()
    by_bounds: dict[tuple, list[Polygon]] = {}
    for polygon, layer, handles, elevation in sorted(closed, key=lambda item: item[0].area, reverse=True):
        key = polygon.normalize().wkb + f":{elevation:.9f}".encode()
        spatial_bounds = (*polygon.bounds, elevation)
        if key in seen_polygons:
            continue
        # Equal rings with different segmentation are duplicates; position still matters.
        if any(polygon.equals(previous) for previous in by_bounds.get(spatial_bounds, [])):
            continue
        seen_polygons.add(key)
        by_bounds.setdefault(spatial_bounds, []).append(polygon)
        coords = list(polygon.exterior.coords)[:-1]
        preview = coords if len(coords) <= 1000 else [coords[int(i * len(coords) / 1000)] for i in range(1000)]
        contained = [t for t in labels if abs(t.elevation_raw - elevation) <= tolerance and polygon.covers(Point(t.position.x, t.position.y))]
        meaningful = [t for t in contained if re.search(r"[A-Za-z\u4e00-\u9fff]{2}", t.text)]
        name = min(meaningful, key=lambda t: polygon.centroid.distance(Point(t.position.x, t.position.y))).text if meaningful else None
        minx, miny, maxx, maxy = polygon.bounds
        candidates.append(FloorPlanAreaCandidate(
            candidate_id=hashlib.sha256(key).hexdigest()[:20], entity_type="POLYLINE", layer=layer,
            raw_area=polygon.area, area_m2=polygon.area * scale**2 if scale else None,
            length_m=(maxx - minx) * scale if scale else None, width_m=(maxy - miny) * scale if scale else None,
            points=[_point(p) for p in preview], boundary=[_point(p) for p in coords],
            holes=[[_point(p) for p in list(r.coords)[:-1]] for r in polygon.interiors],
            source_handles=handles, geometry_sources=[sources[h] for h in handles if h in sources], inferred_name=name, elevation_raw=elevation,
        ))
    elements = []
    for (kind, handle), group in semantic_groups.items():
        points = [point for path in group for point in path.points]
        polygons = []
        exact_closed_geometry = kind not in {"door", "window"} and all(path.closed for path in group)
        if exact_closed_geometry:
            rings = [Polygon([(point.x, point.y) for point in path.points]) for path in group]
            if any(not polygon.is_valid or polygon.area <= 0 for polygon in rings):
                unsupported[kind] += 1
                issues.append(ModelIssue(
                    code="invalid_element_geometry",
                    message=f"构件闭合轮廓无效，不能安全简化：{kind}",
                    source_handle=handle,
                    severity="error",
                ))
                continue
            else:
                # Polygonize nested closed CAD loops with even/odd fill semantics:
                # an inner loop is a void, not a second solid filled object.
                faces = list(ops.polygonize(ops.unary_union([polygon.boundary for polygon in rings])))
                polygons = [face for face in faces
                            if sum(ring.covers(face.representative_point()) for ring in rings) % 2 == 1]
                if not polygons:
                    unsupported[kind] += 1
                    issues.append(ModelIssue(
                        code="invalid_element_geometry",
                        message=f"构件闭合轮廓未形成有效实体：{kind}",
                        source_handle=handle,
                        severity="error",
                    ))
                    continue
                merged = ops.unary_union(polygons)
                polygons = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
                if not all(polygon.geom_type == "Polygon" and polygon.is_valid and polygon.area > 0
                           for polygon in polygons):
                    unsupported[kind] += 1
                    issues.append(ModelIssue(
                        code="invalid_element_geometry",
                        message=f"构件轮廓无法转换为有效 IFC 面：{kind}",
                        source_handle=handle,
                        severity="error",
                    ))
                    continue
        if not polygons:
            outline = (Polygon([(point.x, point.y) for point in points]).minimum_rotated_rectangle
                       if len(points) >= 3 else LineString([(point.x, point.y) for point in points]).envelope)
            polygons = [outline] if outline.geom_type == "Polygon" and outline.area > 0 else []

        for component, polygon in enumerate(polygons, start=1):
            footprint = [_point(point) for point in list(polygon.exterior.coords)[:-1]]
            rotation = None
            if len(footprint) >= 2:
                a, b = footprint[:2]
                rotation = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
            component_handle = f"{handle}/component-{component}" if len(polygons) > 1 else handle
            elements.append(SpatialElement(
                kind=kind,
                name=f"{sources[group[0].source_handle].get('block') or group[0].layer} / {component_handle}",
                footprint=footprint,
                holes=[[_point(point) for point in list(ring.coords)[:-1]] for ring in polygon.interiors],
                rotation_deg=rotation,
                elevation_m=group[0].elevation_raw * scale if scale else None,
                provenance={"footprint": FieldProvenance(
                                source="cad" if exact_closed_geometry else "inferred",
                                locator=handle, confidence=0.9 if exact_closed_geometry else 0.4,
                                note="由闭合 CAD 轮廓合并" if exact_closed_geometry
                                else "从未闭合 CAD 线段推得的最小旋转矩形，需核对"),
                            "holes": FieldProvenance(
                                source="cad" if exact_closed_geometry else "inferred",
                                locator=handle, confidence=0.9 if exact_closed_geometry else 0.4,
                                note="保留闭合 CAD 轮廓中的内环" if polygon.interiors else "原始轮廓无内环"),
                            "kind": FieldProvenance(source="inferred", locator=group[0].layer, confidence=0.5),
                            "elevation_m": FieldProvenance(source="cad", locator=handle)},
            ))
    if unsupported:
        issues.append(ModelIssue(code="incomplete_read", message="存在未完整建模实体，不能声称完整读取"))
    return dict(area_candidates=candidates, elements=elements, layers=sorted({
                    str(e.dxf.get("layer", "0")) if str(e.dxf.get("layer", "0")) != "0" else inherited_layer or "0"
                    for e, _, _, inherited_layer in records}),
                external_references=xrefs, unsupported_entities=dict(unsupported),
                read_complete=not xrefs and not unsupported and not any(i.severity == "error" for i in issues),
                issues=issues, repairs=repairs, drawing_paths=paths, drawing_labels=labels)


def _polygonize_level(contour_paths, elevation, scale, tolerance, issues, repairs):
    closed = []
    segments = []
    seen_segments = set()
    duplicates = 0
    for p in contour_paths:
        coords = [(v.x, v.y) for v in p.points]
        if p.closed:
            coords.append(coords[0])
        for a, b in zip(coords, coords[1:]):
            if a == b:
                continue
            key = tuple(sorted((a, b)))
            if key in seen_segments:
                duplicates += 1
                continue
            seen_segments.add(key)
            segments.append((a, b))
    if duplicates:
        repairs.append(f"移除 {duplicates} 条位置相同的重复线段（原始图仍保留）")
    if segments:
        # Snap only endpoints within 1 mm when the drawing has a known unit.
        snapped = 0
        if scale:
            epsilon = GAP_TOLERANCE_M / scale
            cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
            def endpoint(p):
                nonlocal snapped
                grid = (math.floor(p[0] / epsilon), math.floor(p[1] / epsilon))
                for x in range(grid[0] - 1, grid[0] + 2):
                    for y in range(grid[1] - 1, grid[1] + 2):
                        for previous in cells.get((x, y), []):
                            if math.dist(previous, p) <= epsilon:
                                snapped += int(previous != p)
                                return previous
                cells.setdefault(grid, []).append(p)
                return p
            segments = [(endpoint(a), endpoint(b)) for a, b in segments]
        if snapped:
            repairs.append(f"按 1 mm 容差合并 {snapped} 个断口端点；需在叠图核对")
        network = ops.unary_union([LineString([a, b]) for a, b in segments if a != b])
        polygons, cuts, dangles, invalid = ops.polygonize_full(list(getattr(network, "geoms", [network])))
        for label, geom in (("断开线段", dangles), ("未成面线段", cuts), ("无效环", invalid)):
            if not geom.is_empty:
                issues.append(ModelIssue(code="open_geometry", message=f"{label} {len(geom.geoms)} 处，需核对是否遗漏房间", position=_point(geom.geoms[0].coords[0])))
        source_lines = [LineString([(v.x, v.y) for v in p.points] + ([(p.points[0].x, p.points[0].y)] if p.closed else [])) for p in contour_paths]
        tree = STRtree(source_lines)
        for polygon in polygons.geoms:
            # Provenance includes intersecting contour sources, not a guessed room name.
            source_indices = tree.query(polygon.boundary.buffer(tolerance), predicate="intersects")
            handles = [contour_paths[int(i)].source_handle for i in source_indices]
            source_layers = {contour_paths[int(i)].layer for i in source_indices}
            layer = next(iter(source_layers)) if len(source_layers) == 1 else "CONTOUR"
            closed.append((polygon, layer, handles, elevation))
    return closed
