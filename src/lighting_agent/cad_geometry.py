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
KINDS = {
    "door": r"door|门|dlx_apert", "window": r"window|窗",
    "column": r"column|柱", "furniture": r"furn|desk|chair|table|家具|桌|椅|柜|dlx_obj",
    "obstruction": r"obstruct|遮挡|设备",
}


def _point(p) -> CadPoint:
    return CadPoint(x=float(p[0]), y=float(p[1]))


def inspect_drawing(document, scale: float | None) -> dict[str, Any]:
    issues: list[ModelIssue] = []
    repairs: list[str] = []
    unsupported: Counter[str] = Counter()
    records: list[tuple[Any, str, str]] = []
    visited = 0
    xrefs = [str(block.name) for block in document.blocks
             if bool(int(block.block.dxf.get("flags", 0)) & (4 | 8))]
    for name in xrefs:
        issues.append(ModelIssue(code="external_reference", message=f"外部参照 {name} 未自动载入", severity="error"))

    def expand(entities, chain: str = "", block_name: str = "", depth: int = 0):
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
            if entity.dxftype() == "INSERT":
                name = str(entity.dxf.name)
                if name in xrefs:
                    continue
                def skipped(child, reason):
                    unsupported[child.dxftype()] += 1
                    issues.append(ModelIssue(code="block_transform", message=str(reason), source_handle=handle, severity="error"))
                try:
                    inserts = list(entity.multi_insert()) if entity.mcount > 1 else [entity]
                    for n, insert in enumerate(inserts):
                        expand(insert.virtual_entities(skipped_entity_callback=skipped), f"{handle}[{n}]", name, depth + 1)
                        expand(insert.attribs, f"{handle}[{n}]/attributes", name, depth + 1)
                    repairs.append(f"展开块 {name} ({handle})，保留 WCS 平移/缩放/旋转")
                except Exception as error:
                    issues.append(ModelIssue(code="block_transform", message=f"块 {name}: {error}", source_handle=handle, severity="error"))
                continue
            records.append((entity, handle, block_name))
    expand(document.modelspace())
    tolerance = CURVE_TOLERANCE_M / scale if scale else 0.001
    paths: list[DrawingPath] = []
    labels: list[DrawingLabel] = []
    sources: dict[str, dict] = {}
    semantic_groups: dict[tuple[str, str], list[DrawingPath]] = {}
    semantic_handles: set[str] = set()
    closed: list[tuple[Polygon, str, list[str], float]] = []
    vertices = 0
    for entity, handle, block_name in records:
        kind = entity.dxftype()
        layer = str(entity.dxf.get("layer", "0"))
        if kind in {"TEXT", "MTEXT", "ATTRIB"}:
            value = entity.plain_text() if kind == "MTEXT" else str(entity.dxf.text)
            labels.append(DrawingLabel(text=value, position=_point(entity.dxf.insert), layer=layer, source_handle=handle, elevation_raw=float(entity.dxf.insert.z)))
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
            semantic = next((k for k, pattern in KINDS.items() if re.search(pattern, f"{layer} {block_name}", re.I)), None)
            if semantic:
                semantic_handles.add(handle)
                group = handle.split("]")[0] + "]" if block_name else handle
                semantic_groups.setdefault((semantic, group), []).append(drawing_path)
            if is_closed and len(coords) >= 3 and not semantic:
                polygon = Polygon(coords)
                if not polygon.is_valid or polygon.area <= 0:
                    issues.append(ModelIssue(code="invalid_boundary", message="闭合边界自交或退化，请人工修正", source_handle=handle, position=_point(coords[0])))
                else:
                    closed.append((polygon, layer, [handle], flattened[0].z))
        except Exception as error:
            unsupported[kind] += 1
            issues.append(ModelIssue(code="geometry_error", message=f"{kind}: {error}", source_handle=handle, severity="error"))
            if vertices > MAX_VERTICES:
                break

    preferred = {p.layer for p in paths if re.search(r"wall|cont|墙|房间|room|structural", p.layer, re.I)}
    contour_paths = [p for p in paths if p.source_handle not in semantic_handles and (not preferred or p.layer in preferred) and
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
        pts = [v for p in group for v in p.points]
        rectangle = Polygon([(v.x, v.y) for v in pts]).minimum_rotated_rectangle if len(pts) >= 3 else LineString([(v.x, v.y) for v in pts]).envelope
        footprint = [_point(p) for p in list(rectangle.exterior.coords)[:-1]] if rectangle.geom_type == "Polygon" else pts
        rotation = None
        if len(footprint) >= 2:
            a, b = footprint[:2]
            rotation = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
        elements.append(SpatialElement(kind=kind, name=f"{sources[group[0].source_handle].get('block') or group[0].layer} / {handle}", footprint=footprint, rotation_deg=rotation,
            elevation_m=group[0].elevation_raw * scale if scale else None,
            provenance={"footprint": FieldProvenance(source="inferred", locator=handle, confidence=0.6),
                        "kind": FieldProvenance(source="inferred", locator=group[0].layer, confidence=0.5),
                        "elevation_m": FieldProvenance(source="cad", locator=handle)}))
    if unsupported:
        issues.append(ModelIssue(code="incomplete_read", message="存在未完整建模实体，不能声称完整读取"))
    return dict(area_candidates=candidates, elements=elements, layers=sorted({str(e.dxf.get("layer", "0")) for e, _, _ in records}),
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
            handles = [contour_paths[int(i)].source_handle for i in tree.query(polygon.boundary.buffer(tolerance), predicate="intersects")]
            closed.append((polygon, "CONTOUR", handles, elevation))
    return closed
