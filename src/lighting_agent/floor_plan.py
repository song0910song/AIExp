"""Safe, bounded extraction of 2D lighting-design context from CAD drawings."""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Literal, cast

from ezdxf import bbox
from ezdxf.addons import odafc
from ezdxf.document import Drawing
from ezdxf.filemanagement import readfile
from ezdxf.lldxf.const import DXFError
from ezdxf.math import Vec2
from shapely import LineString, ops

from .schemas import CadPoint, FloorPlan, FloorPlanAreaCandidate, FloorPlanAsset


class FloorPlanParseError(RuntimeError):
    """A drawing could not be safely converted or parsed."""


MAX_TEXT_ITEMS = 200
MAX_AREA_CANDIDATES = 50
MAX_CANDIDATE_POINTS = 1_000
MAX_DRAWING_BYTES = 50 * 1024 * 1024
SUPPORTED_DRAWING_SUFFIXES = frozenset({".dxf", ".dwg"})
# 匹配面积（㎡/平方米）与功率密度（W/m² 或 W/㎡），用于剥离房间名附近的测量标注。
_MEASUREMENT_PATTERN = re.compile(
    r"(?:(?<!\d)\d+(?:[.,]\d+)?\s*(?:m²|㎡|平方米|sq\.?\s*m)|"
    r"\d+(?:[.,]\d+)?\s*W\s*/\s*(?:m²|㎡|m))(?!\w)",
    re.IGNORECASE,
)
_ROOM_NAME_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z]{2,}")
_UNIT_TO_METERS: dict[int, tuple[str, float | None]] = {
    0: ("unitless", None),
    1: ("in", 0.0254),
    2: ("ft", 0.3048),
    4: ("mm", 0.001),
    5: ("cm", 0.01),
    6: ("m", 1.0),
    7: ("km", 1000.0),
}


def parse_floor_plan(source: Path, *, storage_path: str) -> FloorPlan:
    """Extract a normalized, reviewable floor-plan summary from one CAD file.

    DXF is read directly. DWG requires the locally installed ODA File
    Converter, which is invoked only by ezdxf in a temporary directory; the
    original upload is never modified. Geometry is advisory until explicitly
    applied to the design brief.
    """

    source = source.resolve()
    suffix = source.suffix.casefold()
    if suffix not in SUPPORTED_DRAWING_SUFFIXES:
        raise FloorPlanParseError("仅支持 .dxf 与 .dwg 平面图文件")
    if not source.is_file():
        raise FloorPlanParseError("平面图文件不存在")
    try:
        document, converted_from_dwg, warnings = _read_document(source)
    except (DXFError, IOError, OSError, odafc.ODAFCError) as error:
        raise FloorPlanParseError(f"无法解析 CAD 图纸：{error}") from error

    unit_name, meters_per_unit = _drawing_unit(document)
    modelspace = document.modelspace()
    entities = list(modelspace)
    entity_counts = Counter(entity.dxftype() for entity in entities)
    bounds = _bounds(modelspace)
    text_items = _text_items(entities)
    area_candidates = _area_candidates(entities, meters_per_unit)
    room_name = _room_name(text_items)
    if not meters_per_unit:
        warnings.append("图纸未声明可换算的长度单位；面积与尺寸仅能作为原始单位参考。")
    if not area_candidates:
        warnings.append("未识别到闭合房间边界；请在图纸中使用闭合多段线或在界面手动填写面积。")

    return FloorPlan(
        asset=FloorPlanAsset(
            source_name=source.name,
            source_type=cast(Literal["dxf", "dwg"], suffix[1:]),
            storage_path=storage_path,
            sha256=_sha256(source),
            size_bytes=source.stat().st_size,
            converted_from_dwg=converted_from_dwg,
        ),
        drawing_units=unit_name,
        meters_per_drawing_unit=meters_per_unit,
        bounds=bounds,
        entity_counts=dict(sorted(entity_counts.items())),
        text_items=text_items,
        room_name=room_name,
        area_candidates=area_candidates,
        warnings=list(dict.fromkeys(warnings)),
    )


def _read_document(source: Path) -> tuple[Drawing, bool, list[str]]:
    if source.suffix.casefold() == ".dxf":
        return readfile(source), False, []
    if not odafc.is_installed():
        raise FloorPlanParseError(
            "当前主机未安装 ODA File Converter，无法安全转换 DWG。请安装转换器后重试，或将图纸另存为 DXF。"
        )
    with tempfile.TemporaryDirectory(prefix="lighting-dwg-") as temporary_directory:
        converted = Path(temporary_directory) / f"{source.stem}.dxf"
        odafc.convert(source, converted, replace=True)
        return readfile(converted), True, ["DWG 已在本机转换为临时 DXF 后解析；原始 DWG 未被修改。"]


def _drawing_unit(document: Drawing) -> tuple[str, float | None]:
    unit_code = int(document.header.get("$INSUNITS", 0) or 0)
    return _UNIT_TO_METERS.get(unit_code, (f"unknown:{unit_code}", None))


def _bounds(modelspace: Any) -> tuple[CadPoint, CadPoint] | None:
    try:
        extents = bbox.extents(modelspace, cache=bbox.Cache())
    except Exception:
        return None
    if not extents.has_data:
        return None
    return (
        CadPoint(x=round(extents.extmin.x, 6), y=round(extents.extmin.y, 6)),
        CadPoint(x=round(extents.extmax.x, 6), y=round(extents.extmax.y, 6)),
    )


def _text_items(entities: list[Any]) -> list[str]:
    values: list[str] = []
    for entity in entities:
        entity_type = entity.dxftype()
        value = ""
        if entity_type == "TEXT":
            value = str(entity.dxf.text)
        elif entity_type in {"MTEXT", "ATTRIB"}:
            value = str(getattr(entity, "text", "") or getattr(entity.dxf, "text", ""))
        value = " ".join(value.split())
        if value and value not in values:
            values.append(value[:300])
        if len(values) >= MAX_TEXT_ITEMS:
            break
    return values


def _room_name(text_items: list[str]) -> str | None:
    for value in text_items:
        # 功率密度/面积标注常与房间名同现，如 "512会议室 (9.01 W/m²)"。
        # 剥离这些测量信息后再匹配房间名，而不是整段跳过。
        candidate = value
        if "W/m" in candidate or _MEASUREMENT_PATTERN.search(candidate):
            candidate = _MEASUREMENT_PATTERN.sub("", candidate)
            candidate = re.sub(r"\(\s*\)", "", candidate).strip()
            if not candidate:
                continue
        match = _ROOM_NAME_PATTERN.search(candidate)
        if match:
            return match.group(0)
    return None


def _area_candidates(
    entities: list[Any], meters_per_unit: float | None
) -> list[FloorPlanAreaCandidate]:
    # 优先：闭合多段线直接作为房间边界候选。
    candidates = _closed_polyline_candidates(entities, meters_per_unit)
    # 补充：DIALux 等导出的图纸常把墙体画成分段线段网络，用 shapely
    # 重构闭合轮廓，在闭合多段线识别不到面积时提供替代候选。
    candidates += _reconstructed_candidates(entities, meters_per_unit)
    flattened: list[FloorPlanAreaCandidate] = []
    seen: set[tuple[float, float]] = set()
    for candidate in candidates:
        key = (candidate.raw_area, round(candidate.length_m or 0, 4))
        if key in seen:
            continue
        seen.add(key)
        flattened.append(candidate)
    flattened.sort(key=lambda item: item.raw_area, reverse=True)
    return flattened[:MAX_AREA_CANDIDATES]


def _closed_polyline_candidates(
    entities: list[Any], meters_per_unit: float | None
) -> list[FloorPlanAreaCandidate]:
    candidates: list[FloorPlanAreaCandidate] = []
    for entity in entities:
        points = _closed_polyline_points(entity)
        if points is None:
            continue
        candidate = _to_candidate(entity.dxftype(), str(entity.dxf.layer), points, meters_per_unit)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _reconstructed_candidates(
    entities: list[Any], meters_per_unit: float | None
) -> list[FloorPlanAreaCandidate]:
    # 优先从墙体/轮廓图层收集线段（DIALux 导出约定为 DLX_CONT，另含常见墙线层），
    # 避免网格、标注文字等辅助图层的小碎线污染多边形化结果。
    layers = _contour_layers(entities)
    lines: list[Any] = []
    for entity in entities:
        if entity.dxftype() not in {"LINE", "POLYLINE", "LWPOLYLINE"}:
            continue
        if layers and str(entity.dxf.layer) not in layers:
            continue
        points = _segment_points(entity)
        for a, b in _pairs(points):
            if a.distance(b) <= 0.01:
                continue
            lines.append(LineString([(a.x, a.y), (b.x, b.y)]))
    if not lines:
        return []
    merged = ops.unary_union(lines)
    geoms = list(getattr(merged, "geoms", [merged]))
    polys = list(ops.polygonize(geoms))
    polys.sort(key=lambda p: p.area, reverse=True)
    # DIALux 专属轮廓图层（DLX_CONT）坐标约定为米，不受图纸全局单位声明影响；
    # 使用这些图层时直接按米解释，避免 $INSUNITS 声明与实际不符导致面积失真。
    ddx_metric = layers[0].upper().startswith("DLX_") if layers else False
    effective_meters_per_unit = 1.0 if ddx_metric else meters_per_unit
    # 过滤微碎面：仅保留面积不小于主候选千分之一的面。
    largest = polys[0].area if polys else 0.0
    threshold = largest / 1000
    candidates: list[FloorPlanAreaCandidate] = []
    for polygon in polys:
        if polygon.area < threshold:
            break
        ring = polygon.exterior
        coords = list(ring.coords)
        # 去重首尾闭合点，避免鞋带公式/预览出现重复顶点。
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) < 4:
            continue
        candidate = _to_candidate("POLYLINE", layers[0] if layers else "CONTOUR", [Vec2(x, y) for x, y in coords], effective_meters_per_unit)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _contour_layers(entities: list[Any]) -> list[str]:
    """DIALux 等导出的墙体/轮廓图层；无匹配时返回空，表示收集全部实体。"""
    present = {str(entity.dxf.layer) for entity in entities}
    preferred = [layer for layer in ("DLX_CONT", "WALLS", "WALL", "CONT", "Structural") if layer in present]
    if preferred:
        return preferred
    return []


def _segment_points(entity: Any) -> list[Vec2]:
    entity_type = entity.dxftype()
    if entity_type in {"LINE", "XLINE", "RAY"}:
        return [Vec2(entity.dxf.start), Vec2(entity.dxf.end)]
    if entity_type == "LWPOLYLINE":
        return [Vec2(point) for point in entity.get_points("xy")]
    if entity_type == "POLYLINE":
        return [Vec2(vertex.dxf.location) for vertex in entity.vertices]
    return []


def _pairs(points: list[Vec2]) -> list[tuple[Vec2, Vec2]]:
    return list(zip(points, points[1:]))


def _to_candidate(
    entity_type: Literal["LWPOLYLINE", "POLYLINE"],
    layer: str,
    points: list[Vec2],
    meters_per_unit: float | None,
) -> FloorPlanAreaCandidate | None:
    raw_area = _polygon_area(points)
    # 极小面积（含退化共线）在四舍五入后会归零，直接丢弃。
    if raw_area <= 1e-6:
        return None
    x_values = [point.x for point in points]
    y_values = [point.y for point in points]
    length = max(x_values) - min(x_values)
    width = max(y_values) - min(y_values)
    area_m2 = raw_area * meters_per_unit**2 if meters_per_unit else None
    return FloorPlanAreaCandidate(
        entity_type=entity_type,
        layer=layer,
        raw_area=round(raw_area, 6),
        area_m2=round(area_m2, 4) if area_m2 is not None else None,
        length_m=round(length * meters_per_unit, 4) if meters_per_unit else None,
        width_m=round(width * meters_per_unit, 4) if meters_per_unit else None,
        points=_preview_points(points),
    )


def _preview_points(points: list[Vec2]) -> list[CadPoint]:
    """Retain a bounded outline for preview without changing measured geometry."""

    if len(points) > MAX_CANDIDATE_POINTS:
        step = len(points) / MAX_CANDIDATE_POINTS
        points = [points[int(index * step)] for index in range(MAX_CANDIDATE_POINTS)]
    return [CadPoint(x=round(point.x, 6), y=round(point.y, 6)) for point in points]


def _closed_polyline_points(entity: Any) -> list[Vec2] | None:
    entity_type = entity.dxftype()
    if entity_type == "LWPOLYLINE":
        if not entity.closed:
            return None
        return [Vec2(point) for point in entity.get_points("xy")]
    if entity_type == "POLYLINE":
        if not entity.is_closed:
            return None
        points = [Vec2(vertex.dxf.location) for vertex in entity.vertices]
        return points if len(points) >= 3 else None
    return None


def _polygon_area(points: list[Vec2]) -> float:
    return abs(
        sum(
            point.x * next_point.y - next_point.x * point.y
            for point, next_point in zip(points, [*points[1:], points[0]], strict=True)
        )
    ) / 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()