"""Project-scoped orchestration for relayout and fixed-position redesign."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pydantic import Field, model_validator
from shapely.geometry import Polygon

from .calculations.field import calibrate, evaluate, load_fixture_kind, make_fixture
from .dialux_report import cross_validate, parse_dialux_report
from .dxf_analysis import extract_design
from .photometry_assets import PhotometryAssetStore
from .project_store import RevisionConflictError
from .relayout import plan_relayout
from .retrofit import plan_retrofit
from .schemas import (
    DesignArtifact,
    DesignFixtureSpec,
    DesignIteration,
    DesignMetrics,
    DesignRun,
    ProjectState,
    StrictModel,
)


class RedesignError(ValueError):
    pass


class RedesignBaseRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    dxf_source: str | None = Field(default=None, max_length=500)
    report_source: str | None = Field(default=None, max_length=500)
    target_lux: float | None = Field(default=None, gt=0, le=100_000)
    workplane_height_m: float | None = Field(default=None, ge=0, le=10)
    calibration_scale: float | None = Field(default=None, gt=0, le=20)
    max_attempts: int = Field(default=3, ge=1, le=3)


class RelayoutRequest(RedesignBaseRequest):
    design_fixtures: list[DesignFixtureSpec] = Field(min_length=1, max_length=20)
    existing_panel: DesignFixtureSpec | None = None
    existing_downlight: DesignFixtureSpec | None = None
    mounting_height_m: float | None = Field(default=None, gt=0, le=100)
    margin_m: float = Field(default=0.6, ge=0, le=10)

    @model_validator(mode="after")
    def candidate_roles(self) -> "RelayoutRequest":
        if any(item.role != "candidate" for item in self.design_fixtures):
            raise ValueError("design_fixtures must contain candidate fixtures")
        return self


class RetrofitRequest(RedesignBaseRequest):
    existing_panel: DesignFixtureSpec
    existing_downlight: DesignFixtureSpec
    candidate_panels: list[DesignFixtureSpec] = Field(default_factory=list, max_length=20)
    candidate_downlights: list[DesignFixtureSpec] = Field(default_factory=list, max_length=20)
    panel_mounting_height_m: float | None = Field(default=None, gt=0, le=100)
    downlight_mounting_height_m: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def fixture_roles(self) -> "RetrofitRequest":
        if self.existing_panel.role != "existing" or self.existing_downlight.role != "existing":
            raise ValueError("existing_panel and existing_downlight must use role='existing'")
        if any(item.role != "candidate" for item in [*self.candidate_panels, *self.candidate_downlights]):
            raise ValueError("candidate fixture lists must use role='candidate'")
        if not self.candidate_panels and not self.candidate_downlights:
            raise ValueError("at least one retrofit candidate is required")
        return self


def _safe_source(root: Path, source: str, *, suffixes: set[str]) -> Path:
    candidate = Path(source)
    target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise RedesignError("输入文件必须位于当前项目目录内")
    if not target.is_file():
        raise RedesignError(f"输入文件不存在：{source}")
    if target.suffix.casefold() not in suffixes:
        raise RedesignError(f"输入文件类型不受支持：{target.suffix}")
    return target


def _resolve_dxf(root: Path, state: ProjectState, source: str | None) -> Path:
    resolved = source or (state.floor_plan.asset.storage_path if state.floor_plan else None)
    if not resolved:
        raise RedesignError("项目缺少 DXF；请先上传平面图或提供 dxf_source")
    path = _safe_source(root, resolved, suffixes={".dxf"})
    return path


def _resolve_report(root: Path, state: ProjectState, source: str | None) -> Path | None:
    if source:
        return _safe_source(root, source, suffixes={".pdf"})
    for run in reversed(state.simulation_runs):
        if run.source_kind != "dialux_pdf":
            continue
        for artifact in run.artifacts:
            if artifact.file_name.casefold().endswith(".pdf"):
                try:
                    return _safe_source(root, artifact.storage_path, suffixes={".pdf"})
                except RedesignError:
                    continue
    return None


def _fixture_path(
    root: Path,
    state: ProjectState,
    assets: PhotometryAssetStore,
    spec: DesignFixtureSpec,
) -> Path:
    if spec.local_path:
        return _safe_source(root, spec.local_path, suffixes={".ies", ".ldt"})
    if spec.luminaire_id:
        asset = next(
            (item for item in assets.list_assets(state) if item.luminaire_id == spec.luminaire_id),
            None,
        )
        if asset is None or asset.status != "downloaded":
            raise RedesignError(f"灯具 {spec.luminaire_id} 的配光资产尚未下载")
        choices = [item for item in asset.extracted_files if item.file_type in {"ies", "ldt"}]
        if spec.asset_file:
            choices = [item for item in choices if item.relative_path == spec.asset_file]
        if not choices:
            raise RedesignError(f"灯具 {spec.luminaire_id} 没有可解析的 IES/LDT 文件")
        return assets.read_file(state.project_id, choices[0].relative_path)
    if spec.asset_file:
        return _safe_source(root, spec.asset_file, suffixes={".ies", ".ldt"})
    raise RedesignError(f"灯具 {spec.label} 缺少配光来源")


def _product(
    root: Path,
    state: ProjectState,
    assets: PhotometryAssetStore,
    spec: DesignFixtureSpec,
    index: int,
) -> tuple[dict[str, Any], list[str]]:
    path = _fixture_path(root, state, assets, spec)
    kind = load_fixture_kind(path, spec.label, spec.flux_lm, spec.maintenance_factor)
    key = spec.luminaire_id or f"fixture-{index + 1}"
    warnings: list[str] = []
    if abs(kind.flux_lm - spec.flux_lm) / spec.flux_lm > 0.10:
        warnings.append(
            f"{spec.label}: 输入光通量 {spec.flux_lm:g} lm 与配光文件口径 {kind.flux_lm:.1f} lm 不一致，计算采用配光文件。"
        )
    file_power = kind.distribution.power_w
    if file_power is not None and abs(file_power - spec.watts) > 5:
        warnings.append(
            f"{spec.label}: 输入功率 {spec.watts:g} W 与配光文件 {file_power:g} W 偏差超过 5 W。"
        )
    return {
        "key": key, "label": spec.label, "role": spec.role, "watts": spec.watts,
        "kind": kind, "form_note": spec.form_note, "luminaire_id": spec.luminaire_id,
        "asset_path": str(path),
    }, warnings


def _target_and_geometry(
    request: RedesignBaseRequest,
    dxf: dict[str, Any],
    report: dict[str, Any] | None,
) -> tuple[float, float, list[str]]:
    warnings: list[str] = []
    report_target = report["results"].get("target_lx") if report else None
    target = request.target_lux or report_target
    if target is None:
        raise RedesignError("缺少目标平均照度；请上传含目标值的 DIALux 报告或提供 target_lux")
    if request.target_lux and report_target and request.target_lux != report_target:
        warnings.append(
            f"用户显式目标 {request.target_lux:g} lx 覆盖报告目标 {report_target:g} lx。"
        )
    report_workplane = report["summary"].get("workplane_height_m") if report else None
    workplane = request.workplane_height_m if request.workplane_height_m is not None else report_workplane
    if workplane is None:
        workplane = 0.75
        warnings.append("缺少 DIALux 报告工作面高度，回退为显式默认值 0.75 m。")
    if not dxf["eval_grid"]["points"]:
        raise RedesignError("DXF 未重建出评价网格，无法执行逐点照度计算")
    return float(target), float(workplane), warnings


def _report_height(report: dict[str, Any] | None, role: str) -> float | None:
    if not report:
        return None
    positions = report["positions"]["panels" if role == "panel" else "downlights"]
    heights = sorted({round(float(item["z_m"]), 3) for item in positions})
    if len(heights) == 1:
        return heights[0]
    return None


def _baseline_scale(
    request_scale: float | None,
    report: dict[str, Any] | None,
    evaluation_points: list[list[float]],
    panel_points: list[list[float]],
    downlight_points: list[list[float]],
    panel: dict[str, Any] | None,
    downlight: dict[str, Any] | None,
    panel_height: float | None,
    downlight_height: float | None,
    workplane: float,
) -> tuple[float, list[str]]:
    if request_scale is not None:
        return request_scale, []
    if not report:
        return 1.0, ["未提供 DIALux 报告，校准系数回退为 K=1.0。"]
    if panel is None or downlight is None or panel_height is None or downlight_height is None:
        return 1.0, ["缺少现状配光或安装高度，无法用报告 Em 标定，校准系数回退为 K=1.0。"]
    fixtures = [
        *[
            make_fixture(x, y, panel_height, workplane, panel["kind"])
            for x, y in panel_points
        ],
        *[
            make_fixture(x, y, downlight_height, workplane, downlight["kind"])
            for x, y in downlight_points
        ],
    ]
    scale, warning = calibrate(report["results"].get("mean_lx"), evaluate(evaluation_points, fixtures))
    return scale, [warning] if warning else []


def _metrics(value: dict[str, Any], target: float) -> DesignMetrics:
    return DesignMetrics(
        average_lx=value["Em"], minimum_lx=value["Emin"], maximum_lx=value["Emax"],
        uniformity_uo=value["Uo"], diversity_ud=value["Ud"],
        installed_power_w=value["watts"], lpd_w_m2=value["LPD"],
        target_met=value["Em"] >= target,
        overdesign_pct=round((value["Em"] / target - 1) * 100, 1),
    )


def _artifact(root: Path, target: Path, media_type: str) -> DesignArtifact:
    content = target.read_bytes()
    return DesignArtifact(
        name=target.name,
        relative_path=target.relative_to(root).as_posix(),
        media_type=media_type,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _draw_room(axis, polygon) -> None:
    x, y = polygon.exterior.xy
    axis.plot(x, y, color="#202428", linewidth=1.3)
    for interior in polygon.interiors:
        ix, iy = interior.xy
        axis.plot(ix, iy, color="#202428", linewidth=1)


def _plot_layout(path: Path, polygon, existing: dict[str, list], recommended: list[list[float]]) -> None:
    figure, axes = plt.subplots(1, 2, figsize=(12, 6))
    for axis, title in zip(axes, ("Existing layout", "Recommended relayout")):
        _draw_room(axis, polygon)
        axis.set_aspect("equal")
        axis.set_title(title)
    if existing.get("panel"):
        axes[0].scatter(*zip(*existing["panel"]), marker="s", s=24, label="panel")
    if existing.get("downlight"):
        axes[0].scatter(*zip(*existing["downlight"]), marker="o", s=18, label="downlight")
    if recommended:
        axes[1].scatter(*zip(*recommended), marker="s", s=24, color="#d97706")
    axes[0].legend(loc="best")
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _plot_field(path: Path, polygon, points: list[list[float]], series: list[tuple[str, Any]]) -> None:
    figure, axes = plt.subplots(1, len(series), figsize=(6 * len(series), 6), squeeze=False)
    x = [point[0] for point in points]
    y = [point[1] for point in points]
    for axis, (title, values) in zip(axes[0], series):
        scatter = axis.scatter(x, y, c=values, cmap="viridis", s=28)
        _draw_room(axis, polygon)
        axis.set_aspect("equal")
        axis.set_title(title)
        figure.colorbar(scatter, ax=axis, label="E (lx)")
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


class RedesignService:
    def __init__(self, project_store, project_root: Path, assets: PhotometryAssetStore) -> None:
        self.projects = project_store
        self.root = Path(project_root)
        self.assets = assets

    def _inputs(self, state: ProjectState, request: RedesignBaseRequest):
        if state.revision != request.expected_revision:
            raise RevisionConflictError(
                f"Project revision is {state.revision}, but request expected {request.expected_revision}"
            )
        dxf_path = _resolve_dxf(self.root, state, request.dxf_source)
        report_path = _resolve_report(self.root, state, request.report_source)
        dxf = extract_design(dxf_path)
        report = parse_dialux_report(report_path) if report_path else None
        validation = cross_validate(dxf, report) if report else {"checks": {}, "warnings": []}
        target, workplane, warnings = _target_and_geometry(request, dxf, report)
        room = Polygon(dxf["room"]["exterior"], dxf["room"]["interiors"])
        if not room.is_valid:
            room = room.buffer(0)
        return dxf_path, report_path, dxf, report, validation, target, workplane, room, warnings

    def run_relayout(self, request: RelayoutRequest) -> tuple[DesignRun, ProjectState]:
        state = self.projects.get(self._active_project_id)
        (
            dxf_path, report_path, dxf, report, validation, target, workplane, room, warnings,
        ) = self._inputs(state, request)
        run = DesignRun(
            mode="relayout", input_project_revision=state.revision, target_lux=target,
            calibration_scale=request.calibration_scale or 1.0,
            dxf_source=dxf_path.relative_to(self.root).as_posix(),
            report_source=report_path.relative_to(self.root).as_posix() if report_path else None,
            input_sha256={
                "dxf": dxf["source"]["sha256"],
                **({"report": report["source"]["sha256"]} if report else {}),
            },
        )
        fixture_ids = [item.luminaire_id for item in request.design_fixtures if item.luminaire_id]
        fixture_ids += [
            item.luminaire_id
            for item in (request.existing_panel, request.existing_downlight)
            if item and item.luminaire_id
        ]
        downloaded = self.assets.ensure_design_assets(state, fixture_ids, run.run_id) if fixture_ids else []
        asset_hashes = {item.luminaire_id: item.sha256 for item in downloaded if item.sha256}
        existing_panel = existing_downlight = None
        for index, spec in enumerate((request.existing_panel, request.existing_downlight)):
            if spec:
                product, notes = _product(self.root, state, self.assets, spec, index)
                warnings.extend(notes)
                if index == 0:
                    existing_panel = product
                else:
                    existing_downlight = product
        panel_height = _report_height(report, "panel") or (
            request.existing_panel.mounting_height_m if request.existing_panel else None
        )
        downlight_height = _report_height(report, "downlight") or (
            request.existing_downlight.mounting_height_m if request.existing_downlight else None
        )
        scale, scale_warnings = _baseline_scale(
            request.calibration_scale, report, dxf["eval_grid"]["points"],
            dxf["luminaires"]["panel"], dxf["luminaires"]["downlight"],
            existing_panel, existing_downlight, panel_height, downlight_height, workplane,
        )
        run.calibration_scale = scale
        warnings.extend(scale_warnings)
        design_height = request.mounting_height_m or _report_height(report, "panel")
        if design_height is None:
            design_height = next(
                (item.mounting_height_m for item in request.design_fixtures if item.mounting_height_m), None
            )
        if design_height is None:
            raise RedesignError("缺少设计灯具安装高度；请提供报告或 mounting_height_m")
        plans: list[dict[str, Any]] = []
        for attempt, spec in enumerate(request.design_fixtures[: request.max_attempts], start=1):
            product, notes = _product(self.root, state, self.assets, spec, attempt)
            warnings.extend(notes)
            plan = plan_relayout(
                room, dxf["eval_grid"]["points"], product["kind"], target_lux=target,
                mounting_height_m=design_height, workplane_height_m=workplane,
                margin_m=request.margin_m, calibration_scale=scale,
                watts_per_fixture=spec.watts,
            )
            plan["fixture"] = {
                "key": product["key"], "label": product["label"],
                "luminaire_id": product["luminaire_id"], "form_note": product["form_note"],
                "flux_lm": round(product["kind"].flux_lm, 1), "watts": spec.watts,
            }
            plan["_kind"] = product["kind"]
            plans.append(plan)
            verdict = plan["verdict"]
            run.iterations.append(
                DesignIteration(
                    attempt=attempt,
                    candidate_ids=[spec.luminaire_id] if spec.luminaire_id else [],
                    asset_sha256={
                        spec.luminaire_id: asset_hashes[spec.luminaire_id]
                        for _ in [0]
                        if spec.luminaire_id and spec.luminaire_id in asset_hashes
                    },
                    metrics=_metrics(plan["recommended"]["metrics"], target),
                    verdict=(
                        "overdesigned" if verdict["overdesigned"]
                        else "target_met" if verdict["meets"] else "under_target"
                    ),
                    notes=notes,
                )
            )
            if verdict["meets"] and not verdict["overdesigned"]:
                break
        successful = [plan for plan in plans if plan["verdict"]["meets"]]
        chosen = min(
            successful or plans,
            key=lambda plan: (
                plan["verdict"]["overdesigned"],
                abs(plan["recommended"]["metrics"]["Em"] - target),
            ),
        )
        persisted_chosen = {key: value for key, value in chosen.items() if not key.startswith("_")}
        run.result = {
            "geometry": {
                "room_name": dxf["room_name"], "area_m2": dxf["room"]["area_m2"],
                "evaluation_point_count": dxf["eval_grid"]["count"],
                "existing_counts": {key: len(value) for key, value in dxf["luminaires"].items()},
            },
            "cross_validation": validation["checks"],
            "workplane_height_m": workplane, "mounting_height_m": design_height,
            "acceptance_scope": "Em only; Uo and glare are disclosed but not acceptance criteria",
            **persisted_chosen,
        }
        run.warnings = list(dict.fromkeys([*warnings, *validation["warnings"]]))[:100]
        self._write_relayout_artifacts(run, room, dxf, chosen, design_height, workplane, scale)
        run.status = "succeeded"
        run.completed_at = datetime.now(UTC)
        updated = self.projects.add_design_run(state.project_id, request.expected_revision, run)
        return run, updated

    def run_retrofit(self, request: RetrofitRequest) -> tuple[DesignRun, ProjectState]:
        state = self.projects.get(self._active_project_id)
        (
            dxf_path, report_path, dxf, report, validation, target, workplane, room, warnings,
        ) = self._inputs(state, request)
        run = DesignRun(
            mode="retrofit", input_project_revision=state.revision, target_lux=target,
            calibration_scale=request.calibration_scale or 1.0,
            dxf_source=dxf_path.relative_to(self.root).as_posix(),
            report_source=report_path.relative_to(self.root).as_posix() if report_path else None,
            input_sha256={
                "dxf": dxf["source"]["sha256"],
                **({"report": report["source"]["sha256"]} if report else {}),
            },
        )
        specs = [request.existing_panel, request.existing_downlight, *request.candidate_panels, *request.candidate_downlights]
        fixture_ids = [item.luminaire_id for item in specs if item.luminaire_id]
        downloaded = self.assets.ensure_design_assets(state, fixture_ids, run.run_id) if fixture_ids else []
        asset_hashes = {item.luminaire_id: item.sha256 for item in downloaded if item.sha256}
        products: list[dict[str, Any]] = []
        for index, spec in enumerate(specs):
            product, notes = _product(self.root, state, self.assets, spec, index)
            warnings.extend(notes)
            products.append(product)
        existing_panel, existing_downlight = products[0], products[1]
        panel_products = [existing_panel, *products[2 : 2 + len(request.candidate_panels)]]
        downlight_products = [existing_downlight, *products[2 + len(request.candidate_panels) :]]
        panel_height = request.panel_mounting_height_m or _report_height(report, "panel") or request.existing_panel.mounting_height_m
        downlight_height = request.downlight_mounting_height_m or _report_height(report, "downlight") or request.existing_downlight.mounting_height_m
        if panel_height is None or downlight_height is None:
            raise RedesignError("缺少面板灯或筒灯安装高度；请提供报告或显式高度")
        scale, scale_warnings = _baseline_scale(
            request.calibration_scale, report, dxf["eval_grid"]["points"],
            dxf["luminaires"]["panel"], dxf["luminaires"]["downlight"],
            existing_panel, existing_downlight, panel_height, downlight_height, workplane,
        )
        run.calibration_scale = scale
        warnings.extend(scale_warnings)
        plan = plan_retrofit(
            dxf["eval_grid"]["points"], dxf["luminaires"]["panel"], dxf["luminaires"]["downlight"],
            panel_products, downlight_products, target_lux=target, calibration_scale=scale,
            area_m2=room.area, panel_height_m=panel_height, downlight_height_m=downlight_height,
            workplane_height_m=workplane,
        )
        metrics = plan["recommended"]["metrics"]
        run.iterations = [
            DesignIteration(
                attempt=1, candidate_ids=fixture_ids, asset_sha256=asset_hashes,
                metrics=_metrics(metrics, target),
                verdict=(
                    "overdesigned" if plan["verdict"]["overdesigned"]
                    else "target_met" if plan["verdict"]["meets"] else "under_target"
                ),
            )
        ]
        run.result = {
            "geometry": {
                "room_name": dxf["room_name"], "area_m2": dxf["room"]["area_m2"],
                "evaluation_point_count": dxf["eval_grid"]["count"],
                "panel_count": len(dxf["luminaires"]["panel"]),
                "downlight_count": len(dxf["luminaires"]["downlight"]),
            },
            "cross_validation": validation["checks"], "workplane_height_m": workplane,
            "panel_mounting_height_m": panel_height, "downlight_mounting_height_m": downlight_height,
            "acceptance_scope": "Em only; Uo and glare are disclosed but not acceptance criteria",
            **plan,
        }
        run.warnings = list(dict.fromkeys([*warnings, *validation["warnings"]]))[:100]
        self._write_retrofit_artifacts(run, room, dxf, plan, panel_products, downlight_products, panel_height, downlight_height, workplane, scale)
        run.status = "succeeded"
        run.completed_at = datetime.now(UTC)
        updated = self.projects.add_design_run(state.project_id, request.expected_revision, run)
        return run, updated

    def _run_directory(self, run: DesignRun) -> Path:
        target = self.root / f"{self._project_id_from_run(run)}.design-runs" / run.run_id
        target.mkdir(parents=True, exist_ok=False)
        return target

    def _project_id_from_run(self, run: DesignRun) -> str:
        # The root can hold multiple legacy projects; input sources always
        # include a project-specific path, but artifacts use the active id set
        # by the public wrapper below.
        return self._active_project_id

    def _write_relayout_artifacts(self, run, room, dxf, plan, mounting, workplane, scale) -> None:
        directory = self._run_directory(run)
        recommended = plan["recommended"]
        report = self._report_text(run, recommended["metrics"], recommended["name"])
        report_path = directory / "redesign-report.md"
        report_path.write_text(report, encoding="utf-8")
        candidates_path = directory / "candidates.csv"
        _write_csv(
            candidates_path,
            ["name", "columns", "rows", "count", "spacing_x_m", "spacing_y_m", "Em", "Emin", "Emax", "Uo", "Ud", "watts", "LPD", "target_met"],
            [
                [item["name"], item["columns"], item["rows"], item["fixture_count"], *item["spacing_m"],
                 item["metrics"]["Em"], item["metrics"]["Emin"], item["metrics"]["Emax"],
                 item["metrics"]["Uo"], item["metrics"]["Ud"], item["metrics"]["watts"],
                 item["metrics"]["LPD"], item["metrics"]["Em"] >= run.target_lux]
                for item in plan["candidates"]
            ],
        )
        layout_path = directory / "recommended_layout.csv"
        _write_csv(layout_path, ["index", "x_m", "y_m", "mounting_height_m"], [
            [index, x, y, mounting] for index, (x, y) in enumerate(recommended["positions"], start=1)
        ])
        layout_image = directory / "layout_compare.png"
        _plot_layout(layout_image, room, dxf["luminaires"], recommended["positions"])
        fixtures = [
            make_fixture(x, y, mounting, workplane, plan["_kind"])
            for x, y in recommended["positions"]
        ]
        field_path = directory / "heatmap_compare.png"
        values = evaluate(dxf["eval_grid"]["points"], fixtures) * scale
        _plot_field(field_path, room, dxf["eval_grid"]["points"], [(recommended["name"], values)])
        run.artifacts = [
            _artifact(self.root, report_path, "text/markdown"),
            _artifact(self.root, candidates_path, "text/csv"),
            _artifact(self.root, layout_path, "text/csv"),
            _artifact(self.root, layout_image, "image/png"),
            _artifact(self.root, field_path, "image/png"),
        ]

    def _write_retrofit_artifacts(self, run, room, dxf, plan, panels, downlights, panel_height, downlight_height, workplane, scale) -> None:
        directory = self._run_directory(run)
        recommended = plan["recommended"]
        report_path = directory / "redesign-report.md"
        report_path.write_text(self._report_text(run, recommended["metrics"], recommended["name"]), encoding="utf-8")
        scenarios_path = directory / "scenarios.csv"
        _write_csv(
            scenarios_path,
            ["name", "Em", "Emin", "Emax", "Uo", "Ud", "watts", "LPD", "target_met", "over_pct"],
            [[item["name"], item["metrics"]["Em"], item["metrics"]["Emin"], item["metrics"]["Emax"],
              item["metrics"]["Uo"], item["metrics"]["Ud"], item["metrics"]["watts"],
              item["metrics"]["LPD"], item["meets"], item["over_pct"]] for item in plan["scenarios"]],
        )
        mixed_path = directory / "mixed_sweep.csv"
        _write_csv(
            mixed_path,
            ["k", "replaced_indices", "Em", "Uo", "Ud", "watts", "LPD", "target_met"],
            [[item["k"], " ".join(map(str, item["replaced_indices"])), item["metrics"]["Em"],
              item["metrics"]["Uo"], item["metrics"]["Ud"], item["metrics"]["watts"],
              item["metrics"]["LPD"], item["metrics"]["Em"] >= run.target_lux]
             for item in plan["partial_sweep"]],
        )
        replacement_path = directory / "replacement_plan.csv"
        minimal = plan["minimal_partial"]
        points = dxf["luminaires"]["panel" if plan["partial_side"] == "panel" else "downlight"]
        _write_csv(
            replacement_path, ["index", "x_m", "y_m", "side"],
            [[index, points[index][0], points[index][1], plan["partial_side"]]
             for index in (minimal["replaced_indices"] if minimal else [])],
        )
        panel_by_key = {item["key"]: item for item in panels}
        downlight_by_key = {item["key"]: item for item in downlights}
        chosen_panel = panel_by_key[recommended["panel_key"]]
        chosen_downlight = downlight_by_key[recommended["downlight_key"]]
        fixtures = [
            *[make_fixture(x, y, panel_height, workplane, chosen_panel["kind"]) for x, y in dxf["luminaires"]["panel"]],
            *[make_fixture(x, y, downlight_height, workplane, chosen_downlight["kind"]) for x, y in dxf["luminaires"]["downlight"]],
        ]
        values = evaluate(dxf["eval_grid"]["points"], fixtures) * scale
        image_path = directory / "heatmap_keep.png"
        _plot_field(image_path, room, dxf["eval_grid"]["points"], [(recommended["name"], values)])
        run.artifacts = [
            _artifact(self.root, report_path, "text/markdown"),
            _artifact(self.root, scenarios_path, "text/csv"),
            _artifact(self.root, mixed_path, "text/csv"),
            _artifact(self.root, replacement_path, "text/csv"),
            _artifact(self.root, image_path, "image/png"),
        ]

    @staticmethod
    def _report_text(run: DesignRun, metrics: dict[str, Any], recommendation: str) -> str:
        verdict = "达标" if metrics["Em"] >= run.target_lux else "未达标"
        warning_lines = "\n".join(f"- {item}" for item in run.warnings) or "- 无"
        return "\n".join(
            [
                f"# 照明重设计报告 · {run.mode}", "",
                f"- 结论：**{verdict}**，推荐 `{recommendation}`。",
                f"- 平均照度 Em：**{metrics['Em']:.1f} lx**（目标 {run.target_lux:.1f} lx）。",
                f"- 均匀度 Uo：{metrics['Uo']:.3f}（披露项，本轮不作为验收条件）。",
                f"- 总功率：{metrics['watts']:.1f} W；LPD：{metrics['LPD']:.2f} W/m²。",
                f"- 室内增益校准系数 K：{run.calibration_scale:.4f}。", "",
                "## 警告与限制", "", warning_lines,
                "- 当前求解使用真实配光逐点直射分量和单一 K 校准；最终施工前必须在 DIALux evo 或等效专业软件中复算。",
                "- 当前未可靠计算 UGR、家具遮挡与非均匀互反射。", "",
            ]
        )

    def bind_project(self, project_id: str) -> "RedesignService":
        self._active_project_id = project_id
        return self


def run_relayout(
    project_store,
    project_root: Path,
    assets: PhotometryAssetStore,
    project_id: str,
    request: RelayoutRequest,
) -> tuple[DesignRun, ProjectState]:
    return RedesignService(project_store, project_root, assets).bind_project(project_id).run_relayout(request)


def run_retrofit(
    project_store,
    project_root: Path,
    assets: PhotometryAssetStore,
    project_id: str,
    request: RetrofitRequest,
) -> tuple[DesignRun, ProjectState]:
    return RedesignService(project_store, project_root, assets).bind_project(project_id).run_retrofit(request)


__all__ = [
    "RedesignError", "RedesignService", "RelayoutRequest", "RetrofitRequest",
    "run_relayout", "run_retrofit",
]
