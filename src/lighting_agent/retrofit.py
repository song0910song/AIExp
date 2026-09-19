"""Fixed-position product replacement planner."""

from __future__ import annotations

from typing import Any

import numpy as np

from .calculations.field import FixtureKind, evaluate, make_fixture, summarize


def _fixtures(
    panel_points: list[list[float]],
    downlight_points: list[list[float]],
    panel: dict[str, Any],
    downlight: dict[str, Any],
    *,
    panel_height_m: float,
    downlight_height_m: float,
    workplane_height_m: float,
    panel_overrides: dict[int, dict[str, Any]] | None = None,
    downlight_overrides: dict[int, dict[str, Any]] | None = None,
) -> list:
    result = []
    for index, (x, y) in enumerate(panel_points):
        product = (panel_overrides or {}).get(index, panel)
        result.append(make_fixture(x, y, panel_height_m, workplane_height_m, product["kind"]))
    for index, (x, y) in enumerate(downlight_points):
        product = (downlight_overrides or {}).get(index, downlight)
        result.append(make_fixture(x, y, downlight_height_m, workplane_height_m, product["kind"]))
    return result


def required_uniform_flux(
    evaluation_points: list[list[float]],
    panel_points: list[list[float]],
    downlight_points: list[list[float]],
    existing_panel: dict[str, Any],
    existing_downlight: dict[str, Any],
    *,
    target_lux: float,
    calibration_scale: float,
    panel_height_m: float,
    downlight_height_m: float,
    workplane_height_m: float,
) -> dict[str, float]:
    base: FixtureKind = existing_panel["kind"]
    probe = FixtureKind(
        label=f"{base.label}-1000lm",
        distribution=base.distribution,
        flux_lm=1000.0,
        maintenance_factor=base.maintenance_factor,
        source_path=base.source_path,
    )
    panel_1k = evaluate(
        evaluation_points,
        [make_fixture(x, y, panel_height_m, workplane_height_m, probe) for x, y in panel_points],
    )
    down_only = evaluate(
        evaluation_points,
        [
            make_fixture(x, y, downlight_height_m, workplane_height_m, existing_downlight["kind"])
            for x, y in downlight_points
        ],
    )
    panel_mean = float(panel_1k.mean())
    if panel_mean <= 0:
        raise ValueError("面板 1000 lm 探针场均值为 0，无法反解")
    required = 1000.0 * (target_lux / calibration_scale - float(down_only.mean())) / panel_mean
    return {"required_flux_lm": round(max(0.0, required), 1), "panel_1k_raw_lx": panel_mean}


def sweep_combinations(
    evaluation_points: list[list[float]],
    panel_points: list[list[float]],
    downlight_points: list[list[float]],
    panels: list[dict[str, Any]],
    downlights: list[dict[str, Any]],
    *,
    target_lux: float,
    calibration_scale: float,
    area_m2: float,
    panel_height_m: float,
    downlight_height_m: float,
    workplane_height_m: float,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for panel in panels:
        for downlight in downlights:
            fixtures = _fixtures(
                panel_points, downlight_points, panel, downlight,
                panel_height_m=panel_height_m, downlight_height_m=downlight_height_m,
                workplane_height_m=workplane_height_m,
            )
            watts = len(panel_points) * panel["watts"] + len(downlight_points) * downlight["watts"]
            metrics = summarize(evaluate(evaluation_points, fixtures) * calibration_scale, watts, area_m2)
            result.append(
                {
                    "name": f"{panel['key']} + {downlight['key']}",
                    "panel_key": panel["key"], "downlight_key": downlight["key"],
                    "metrics": metrics, "meets": metrics["Em"] >= target_lux,
                    "over_pct": round((metrics["Em"] / target_lux - 1) * 100, 1),
                }
            )
    return result


def select_combination(scenarios: list[dict[str, Any]], target_lux: float) -> dict[str, Any]:
    meeting = [item for item in scenarios if item["metrics"]["Em"] >= target_lux]
    within_band = [item for item in meeting if item["metrics"]["Em"] <= 1.2 * target_lux]
    if within_band:
        return max(within_band, key=lambda item: (item["metrics"]["Uo"], -item["metrics"]["watts"]))
    if meeting:
        return min(meeting, key=lambda item: item["metrics"]["Em"])
    if not scenarios:
        raise ValueError("没有可评估的替换组合")
    return max(scenarios, key=lambda item: item["metrics"]["Em"])


def sweep_partial(
    evaluation_points: list[list[float]],
    panel_points: list[list[float]],
    downlight_points: list[list[float]],
    existing_panel: dict[str, Any],
    existing_downlight: dict[str, Any],
    chosen_panel: dict[str, Any],
    chosen_downlight: dict[str, Any],
    *,
    calibration_scale: float,
    area_m2: float,
    panel_height_m: float,
    downlight_height_m: float,
    workplane_height_m: float,
) -> tuple[str, list[dict[str, Any]]]:
    replace_panels = chosen_panel["key"] != existing_panel["key"]
    points = panel_points if replace_panels else downlight_points
    order = sorted(range(len(points)), key=lambda index: (points[index][1], points[index][0]))
    result: list[dict[str, Any]] = []
    for count in range(len(points) + 1):
        selected_order_indices = (
            set(np.linspace(0, len(order) - 1, count).round().astype(int).tolist()) if count else set()
        )
        selected = {order[index] for index in selected_order_indices}
        panel_overrides = (
            {index: chosen_panel for index in selected} if replace_panels else None
        )
        downlight_overrides = (
            {index: chosen_downlight for index in selected} if not replace_panels else None
        )
        panel_base = existing_panel if replace_panels else chosen_panel
        downlight_base = chosen_downlight if replace_panels else existing_downlight
        fixtures = _fixtures(
            panel_points, downlight_points, panel_base, downlight_base,
            panel_height_m=panel_height_m, downlight_height_m=downlight_height_m,
            workplane_height_m=workplane_height_m,
            panel_overrides=panel_overrides, downlight_overrides=downlight_overrides,
        )
        panel_watts = sum(
            (chosen_panel if replace_panels and index in selected else panel_base)["watts"]
            for index in range(len(panel_points))
        )
        downlight_watts = sum(
            (chosen_downlight if not replace_panels and index in selected else downlight_base)["watts"]
            for index in range(len(downlight_points))
        )
        metrics = summarize(
            evaluate(evaluation_points, fixtures) * calibration_scale,
            panel_watts + downlight_watts,
            area_m2,
        )
        result.append({"k": count, "replaced_indices": sorted(selected), "metrics": metrics})
    return ("panel" if replace_panels else "downlight"), result


def select_minimal(partial: list[dict[str, Any]], target_lux: float) -> dict[str, Any] | None:
    first_index = next(
        (index for index, item in enumerate(partial) if item["metrics"]["Em"] >= target_lux), None
    )
    if first_index is None:
        return None
    chosen = partial[first_index]
    if first_index + 1 < len(partial):
        following = partial[first_index + 1]
        current_uo = chosen["metrics"]["Uo"]
        next_uo = following["metrics"]["Uo"]
        if next_uo - current_uo >= 0.03 and next_uo >= current_uo * 1.5:
            chosen = following
    return chosen


def plan_retrofit(
    evaluation_points: list[list[float]],
    panel_points: list[list[float]],
    downlight_points: list[list[float]],
    panels: list[dict[str, Any]],
    downlights: list[dict[str, Any]],
    *,
    target_lux: float,
    calibration_scale: float,
    area_m2: float,
    panel_height_m: float,
    downlight_height_m: float,
    workplane_height_m: float,
) -> dict[str, Any]:
    existing_panel = next(item for item in panels if item.get("role") == "existing")
    existing_downlight = next(item for item in downlights if item.get("role") == "existing")
    required = required_uniform_flux(
        evaluation_points, panel_points, downlight_points, existing_panel, existing_downlight,
        target_lux=target_lux, calibration_scale=calibration_scale,
        panel_height_m=panel_height_m, downlight_height_m=downlight_height_m,
        workplane_height_m=workplane_height_m,
    )
    scenarios = sweep_combinations(
        evaluation_points, panel_points, downlight_points, panels, downlights,
        target_lux=target_lux, calibration_scale=calibration_scale, area_m2=area_m2,
        panel_height_m=panel_height_m, downlight_height_m=downlight_height_m,
        workplane_height_m=workplane_height_m,
    )
    chosen = select_combination(scenarios, target_lux)
    panel_by_key = {item["key"]: item for item in panels}
    downlight_by_key = {item["key"]: item for item in downlights}
    side, partial = sweep_partial(
        evaluation_points, panel_points, downlight_points, existing_panel, existing_downlight,
        panel_by_key[chosen["panel_key"]], downlight_by_key[chosen["downlight_key"]],
        calibration_scale=calibration_scale, area_m2=area_m2,
        panel_height_m=panel_height_m, downlight_height_m=downlight_height_m,
        workplane_height_m=workplane_height_m,
    )
    minimal = select_minimal(partial, target_lux)
    return {
        "target_lux": target_lux, "required_panel_flux": required,
        "scenarios": scenarios, "recommended": chosen,
        "partial_side": side, "partial_sweep": partial, "minimal_partial": minimal,
        "verdict": {
            "average_lx": chosen["metrics"]["Em"], "meets": chosen["meets"],
            "overdesigned": chosen["metrics"]["Em"] > 1.2 * target_lux,
            "over_pct": chosen["over_pct"],
        },
    }


__all__ = [
    "plan_retrofit", "required_uniform_flux", "select_combination", "select_minimal",
    "sweep_combinations", "sweep_partial",
]
