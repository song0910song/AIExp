"""Structured extraction from DIALux-exported DXF drawings."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import ezdxf
from shapely.geometry import LineString, Point
from shapely.ops import polygonize

try:
    from shapely import union_all as _union_all
except ImportError:  # pragma: no cover
    from shapely.ops import unary_union as _union_all

ROOM_LAYER = "DLX_CONT"
LUMINAIRE_LAYER = "DLX_LUM"
GRID_LAYER = "DLX_PBP_EPERADAPT"
DESC_LAYER = "DLX_DESC"
SCHEDULE_BLOCK = "*T0"
RESULT_BLOCK = "*T1"
MAX_DXF_BYTES = 50 * 1024 * 1024
MAX_ENTITIES = 1_000_000
MAX_OUTPUT_POINTS = 20_000


class DxfAnalysisError(ValueError):
    pass


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, value: int) -> int:
        root = value
        while self.parent.setdefault(root, root) != root:
            root = self.parent[root]
        while self.parent[value] != root:
            self.parent[value], value = root, self.parent[value]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _layer_segments(modelspace, layer: str) -> list[tuple[Any, Any]]:
    result: list[tuple[Any, Any]] = []
    for entity in modelspace:
        if entity.dxftype() != "POLYLINE" or str(entity.dxf.get("layer", "")) != layer:
            continue
        points = [vertex.dxf.location for vertex in entity.vertices]
        if len(points) >= 2:
            result.append((points[0], points[-1]))
    return result


def _connected_components(segments, tolerance_m: float = 0.005) -> list[dict[str, float]]:
    union_find = _UnionFind()
    point_owner: dict[tuple[int, int], int] = {}
    for index, segment in enumerate(segments):
        for point in segment:
            key = (round(float(point.x) / tolerance_m), round(float(point.y) / tolerance_m))
            if key in point_owner:
                union_find.union(index, point_owner[key])
            else:
                point_owner[key] = index
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(segments)):
        groups[union_find.find(index)].append(index)
    components: list[dict[str, float]] = []
    for indices in groups.values():
        xs = [float(point.x) for index in indices for point in segments[index]]
        ys = [float(point.y) for index in indices for point in segments[index]]
        components.append(
            {
                "n": float(len(indices)),
                "cx": (min(xs) + max(xs)) / 2,
                "cy": (min(ys) + max(ys)) / 2,
                "w": max(xs) - min(xs),
                "h": max(ys) - min(ys),
            }
        )
    return components


def _cluster(values: list[float], gap: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] < gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _room(modelspace):
    segments = [
        ((float(left.x), float(left.y)), (float(right.x), float(right.y)))
        for left, right in _layer_segments(modelspace, ROOM_LAYER)
    ]
    if not segments:
        raise DxfAnalysisError(f"图层 {ROOM_LAYER} 中没有可用的房间轮廓")
    faces = sorted(
        polygonize(_union_all([LineString(segment) for segment in segments])),
        key=lambda polygon: -polygon.area,
    )
    if not faces:
        raise DxfAnalysisError("无法从 DLX_CONT 墙线重建闭合房间轮廓")
    room = faces[0]
    if not room.is_valid:
        room = room.buffer(0)
    return room, [round(face.area, 2) for face in faces[:6]]


def _luminaires(modelspace) -> dict[str, list[list[float]]]:
    panels: list[list[float]] = []
    downlights: list[list[float]] = []
    for component in _connected_components(_layer_segments(modelspace, LUMINAIRE_LAYER)):
        width, height, count = component["w"], component["h"], component["n"]
        point = [round(component["cx"], 3), round(component["cy"], 3)]
        if 0.5 <= width <= 0.7 and 0.5 <= height <= 0.7 and count >= 16:
            panels.append(point)
        elif 0.12 <= width <= 0.25 and 0.12 <= height <= 0.25 and count >= 40:
            downlights.append(point)
    panels.sort(key=lambda item: (-item[1], item[0]))
    downlights.sort(key=lambda item: (-item[1], item[0]))
    return {"panel": panels[:MAX_OUTPUT_POINTS], "downlight": downlights[:MAX_OUTPUT_POINTS]}


def _evaluation_grid(modelspace, room) -> dict[str, Any]:
    segments = [
        ((float(left.x), float(left.y)), (float(right.x), float(right.y)))
        for left, right in _layer_segments(modelspace, GRID_LAYER)
    ]
    if not segments:
        return {"rows": 0, "cols": 0, "points": [], "count": 0, "source": "none"}
    midpoints = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in segments]
    rows = _cluster([point[1] for point in midpoints], 0.25)
    columns = _cluster([point[0] for point in midpoints], 0.30)
    inside = room.buffer(-0.05)
    points = [
        [round(x, 3), round(y, 3)]
        for y in rows
        for x in columns
        if inside.contains(Point(x, y))
    ][:MAX_OUTPUT_POINTS]
    return {
        "rows": len(rows),
        "cols": len(columns),
        "points": points,
        "count": len(points),
        "source": "DLX_PBP_EPERADAPT 标签聚类",
    }


def _block_rows(document, block_name: str) -> list[list[str]]:
    block = document.blocks.get(block_name)
    if block is None:
        return []
    items: list[tuple[float, float, str]] = []
    for entity in block:
        if entity.dxftype() == "MTEXT":
            items.append((entity.dxf.insert.x, entity.dxf.insert.y, entity.plain_text()))
        elif entity.dxftype() == "TEXT":
            items.append((entity.dxf.insert.x, entity.dxf.insert.y, entity.dxf.text))
    rows: list[list[Any]] = []
    for x, y, value in sorted(items, key=lambda item: (-item[1], item[0])):
        if rows and abs(rows[-1][0] - y) < 0.3:
            rows[-1][1].append((x, value))
        else:
            rows.append([y, [(x, value)]])
    return [[value for _, value in sorted(cells)] for _, cells in rows]


def _number(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    return float(match.group().replace(",", "")) if match else None


def _schedule(rows: list[list[str]]) -> list[dict[str, Any]]:
    start = next((index for index, row in enumerate(rows) if any("索引" in cell for cell in row)), None)
    if start is None:
        return []
    result: list[dict[str, Any]] = []
    for row in rows[start + 1 :]:
        if len(row) < 9:
            continue
        flux, watts, quantity = _number(row[5]), _number(row[7]), _number(row[8])
        if flux is None or watts is None or quantity is None:
            continue
        result.append(
            {
                "index": row[0], "maker": row[1], "name": row[2], "code": row[3],
                "lamp": row[4], "flux_lm": flux, "mf": _number(row[6]) or 1.0,
                "watts": watts, "qty": int(quantity),
            }
        )
    return result[:100]


def _results(rows: list[list[str]]) -> dict[str, Any] | None:
    for row in rows:
        if len(row) >= 8 and any("lx" in cell for cell in row):
            return {
                "name": row[1], "parameter": row[2], "min_lx": _number(row[3]),
                "max_lx": _number(row[4]), "avg_lx": _number(row[5]),
                "Uo": _number(row[6]), "Ud": _number(row[7]),
            }
    return None


def _room_name(modelspace) -> str:
    for entity in modelspace:
        if entity.dxftype() == "TEXT" and str(entity.dxf.layer) == DESC_LAYER:
            return re.split(r"\s*\(", str(entity.dxf.text), maxsplit=1)[0].strip()
    return ""


def extract_design(path: Path) -> dict[str, Any]:
    """Return a bounded JSON-safe geometry snapshot."""

    path = Path(path)
    if not path.is_file():
        raise DxfAnalysisError("DXF 文件不存在")
    if path.stat().st_size > MAX_DXF_BYTES:
        raise DxfAnalysisError("DXF 文件不能超过 50 MB")
    try:
        document = ezdxf.readfile(path)
    except Exception as error:
        raise DxfAnalysisError(f"无法读取 DXF：{error}") from error
    modelspace = document.modelspace()
    if len(modelspace) > MAX_ENTITIES:
        raise DxfAnalysisError("DXF 实体数量超过安全上限")
    room, face_areas = _room(modelspace)
    luminaires = _luminaires(modelspace)
    grid = _evaluation_grid(modelspace, room)
    schedule = _schedule(_block_rows(document, SCHEDULE_BLOCK))
    results = _results(_block_rows(document, RESULT_BLOCK))
    warnings: list[str] = []
    if not luminaires["panel"] and not luminaires["downlight"]:
        warnings.append("未从 DLX_LUM 图层识别到面板灯或筒灯符号。")
    if not grid["points"]:
        warnings.append("未重建出 DIALux 评价网格。")
    return {
        "source": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
            "dxfversion": document.dxfversion,
        },
        "room_name": _room_name(modelspace),
        "room": {
            "area_m2": round(room.area, 2),
            "bounds": [round(value, 3) for value in room.bounds],
            "exterior": [[round(x, 3), round(y, 3)] for x, y in room.exterior.coords],
            "interiors": [
                [[round(x, 3), round(y, 3)] for x, y in ring.coords]
                for ring in room.interiors
            ],
            "faces_area_sanity": face_areas,
        },
        "luminaires": luminaires,
        "eval_grid": grid,
        "schedule": schedule,
        "dialux_results": results,
        "total_watts": sum(item["watts"] * item["qty"] for item in schedule) if schedule else None,
        "warnings": warnings,
    }


__all__ = ["DxfAnalysisError", "extract_design"]
