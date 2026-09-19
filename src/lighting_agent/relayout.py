"""Free-position luminaire redesign planner."""

from __future__ import annotations

from typing import Any

import numpy as np
from shapely.geometry import Point

from .calculations.field import FixtureKind, evaluate, make_fixture, summarize


def uniform_grid(room_polygon, columns: int, rows: int, margin_m: float = 0.6) -> tuple[list[list[float]], tuple[float, float]]:
    """Place fixtures at cell centres inside the inset usable polygon."""

    if columns < 1 or rows < 1:
        raise ValueError("网格行列数必须为正整数")
    usable = room_polygon.buffer(-margin_m)
    if usable.is_empty:
        raise ValueError("墙边退让距离过大，可用区域为空")
    min_x, min_y, max_x, max_y = usable.bounds
    width, height = max_x - min_x, max_y - min_y
    xs = min_x + (np.arange(columns) + 0.5) * width / columns
    ys = min_y + (np.arange(rows) + 0.5) * height / rows
    points = [
        [round(float(x), 3), round(float(y), 3)]
        for y in ys
        for x in xs
        if usable.contains(Point(float(x), float(y)))
    ]
    return points, (float(width / columns), float(height / rows))


def candidate_configs() -> list[dict[str, Any]]:
    return [
        {"name": "P12_2x6", "columns": 2, "rows": 6},
        {"name": "P14_2x7", "columns": 2, "rows": 7},
        {"name": "P12_3x4", "columns": 3, "rows": 4},
        {"name": "P15_3x5", "columns": 3, "rows": 5},
        {"name": "P18_3x6", "columns": 3, "rows": 6},
        {"name": "P21_3x7", "columns": 3, "rows": 7},
        {"name": "P16_4x4", "columns": 4, "rows": 4},
        {"name": "P20_4x5", "columns": 4, "rows": 5},
        {"name": "P24_4x6", "columns": 4, "rows": 6},
        {"name": "P16_2x8", "columns": 2, "rows": 8},
        {"name": "P24_3x8", "columns": 3, "rows": 8},
        {"name": "P25_5x5", "columns": 5, "rows": 5},
        {"name": "P28_4x7", "columns": 4, "rows": 7},
        {"name": "P30_5x6", "columns": 5, "rows": 6},
        {"name": "P32_4x8", "columns": 4, "rows": 8},
        {"name": "P35_5x7", "columns": 5, "rows": 7},
        {"name": "P36_6x6", "columns": 6, "rows": 6},
        {"name": "P40_5x8", "columns": 5, "rows": 8},
    ]


def evaluate_configs(
    room_polygon,
    evaluation_points: list[list[float]],
    kind: FixtureKind,
    *,
    mounting_height_m: float,
    workplane_height_m: float,
    margin_m: float,
    calibration_scale: float,
    watts_per_fixture: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for config in candidate_configs():
        positions, spacing = uniform_grid(
            room_polygon, config["columns"], config["rows"], margin_m
        )
        fixtures = [
            make_fixture(x, y, mounting_height_m, workplane_height_m, kind)
            for x, y in positions
        ]
        values = evaluate(evaluation_points, fixtures) * calibration_scale
        metrics = summarize(values, len(fixtures) * watts_per_fixture, room_polygon.area)
        rows.append(
            {
                **config,
                "fixture_count": len(fixtures),
                "spacing_m": [round(spacing[0], 3), round(spacing[1], 3)],
                "positions": positions,
                "metrics": metrics,
            }
        )
    return rows


def select_recommendation(candidates: list[dict[str, Any]], target_lux: float) -> dict[str, Any]:
    if not candidates:
        raise ValueError("没有可评估的重排候选")
    preferred = [
        item for item in candidates if target_lux <= item["metrics"]["Em"] <= 1.2 * target_lux
    ]
    if preferred:
        return max(preferred, key=lambda item: (item["metrics"]["Uo"], -item["metrics"]["watts"]))
    near = [
        item for item in candidates if 0.9 * target_lux <= item["metrics"]["Em"] <= 1.2 * target_lux
    ]
    if near:
        return max(near, key=lambda item: (item["metrics"]["Uo"], item["metrics"]["Em"]))
    return min(candidates, key=lambda item: abs(item["metrics"]["Em"] - target_lux))


def plan_relayout(
    room_polygon,
    evaluation_points: list[list[float]],
    kind: FixtureKind,
    *,
    target_lux: float,
    mounting_height_m: float,
    workplane_height_m: float,
    margin_m: float = 0.6,
    calibration_scale: float = 1.0,
    watts_per_fixture: float = 0.0,
) -> dict[str, Any]:
    candidates = evaluate_configs(
        room_polygon,
        evaluation_points,
        kind,
        mounting_height_m=mounting_height_m,
        workplane_height_m=workplane_height_m,
        margin_m=margin_m,
        calibration_scale=calibration_scale,
        watts_per_fixture=watts_per_fixture,
    )
    recommended = select_recommendation(candidates, target_lux)
    average = recommended["metrics"]["Em"]
    return {
        "target_lux": target_lux,
        "candidates": candidates,
        "recommended": recommended,
        "verdict": {
            "average_lx": average,
            "meets": average >= target_lux,
            "overdesigned": average > 1.2 * target_lux,
            "over_pct": round((average / target_lux - 1) * 100, 1),
        },
    }


__all__ = [
    "candidate_configs",
    "evaluate_configs",
    "plan_relayout",
    "select_recommendation",
    "uniform_grid",
]
