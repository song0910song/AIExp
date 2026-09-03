"""Deterministic DXF/PDF luminaire placement matching."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from ..schemas import (
    FloorPlan,
    LayoutCheck,
    LayoutIssue,
    LayoutAnalysis,
    LuminairePlacement,
    LuminaireReport,
    SimilarityTransform,
)


DEFAULT_COORDINATE_TOLERANCE_M = 0.05


@dataclass(frozen=True, slots=True)
class _Pair:
    dxf: LuminairePlacement
    report: LuminairePlacement


def analyze_luminaire_layout(
    floor_plan: FloorPlan,
    report: LuminaireReport,
    *,
    coordinate_tolerance_m: float = DEFAULT_COORDINATE_TOLERANCE_M,
) -> LayoutAnalysis:
    """Match DXF symbol candidates to explicit PDF X/Y/Z rows.

    The returned coordinates use the report's metre values when a row is
    matched. The similarity transform and residuals remain visible so the
    result can be reviewed instead of silently rewriting CAD coordinates.
    """

    if coordinate_tolerance_m <= 0:
        raise ValueError("coordinate_tolerance_m must be positive")
    dxf_placements = list(floor_plan.luminaire_placements)
    report_placements = list(report.placements)
    pairs, unmatched_dxf, unmatched_report = _match_placements(dxf_placements, report_placements)
    transform = _fit_transform(pairs, floor_plan.drawing_units)

    output: list[LuminairePlacement] = []
    residuals: list[float] = []
    output_ids_by_dxf_id: dict[str, str] = {}
    for number, dxf in enumerate(dxf_placements, start=1):
        pair = next((item for item in pairs if item.dxf.placement_id == dxf.placement_id), None)
        output_id = f"L-{number:03d}"
        output_ids_by_dxf_id[dxf.placement_id] = output_id
        if pair is None:
            placement = dxf.model_copy(
                update={
                    "placement_id": output_id,
                    "matching_status": "missing_in_report",
                    "confidence": "medium",
                    "source_refs": _source_refs(dxf),
                }
            )
            output.append(placement)
            continue

        residual = _residual(transform, pair.dxf, pair.report) if transform else _distance(pair.dxf, pair.report)
        residuals.append(residual)
        placement = pair.dxf.model_copy(
            update={
                "placement_id": output_id,
                "luminaire_id": pair.report.luminaire_id,
                "model": pair.report.model,
                "manufacturer": pair.report.manufacturer,
                "product_code": pair.report.product_code,
                "x_m": pair.report.x_m,
                "y_m": pair.report.y_m,
                "z_m": pair.report.z_m,
                "report_index": pair.report.report_index,
                "report_page": pair.report.report_page,
                "source_refs": _merge_refs(dxf.source_refs, pair.report.source_refs),
                "coordinate_residual_m": round(residual, 6),
                "matching_status": (
                    "matched" if residual <= coordinate_tolerance_m else "coordinate_mismatch"
                ),
                "confidence": "high" if residual <= coordinate_tolerance_m else "medium",
            }
        )
        output.append(placement)

    for number, report_placement in enumerate(unmatched_report, start=len(output) + 1):
        output.append(
            report_placement.model_copy(
                update={
                    "placement_id": f"L-{number:03d}",
                    "matching_status": "missing_in_dxf",
                    "confidence": "high",
                }
            )
        )

    issues: list[LayoutIssue] = []
    for pair in pairs:
        report_number = pair.report.report_index or len(issues) + 1
        output_id = output_ids_by_dxf_id[pair.dxf.placement_id]
        residual = next(
            item.coordinate_residual_m
            for item in output
            if item.placement_id == output_id
        )
        if residual is not None and residual > coordinate_tolerance_m:
            issues.append(
                LayoutIssue(
                    issue_id=f"layout-coordinate-{report_number:03d}",
                    severity="warning",
                    kind="coordinate_mismatch",
                    placement_ids=[output_id],
                    location=_location(pair.report),
                    message=f"DXF 与报告坐标残差为 {residual:g} m，超过 {coordinate_tolerance_m:g} m 容差。",
                    required_action="确认图纸与报告是否为同一版本，并复核原点、比例、旋转和单位。",
                    confidence="high",
                    source_refs=_merge_refs(pair.dxf.source_refs, pair.report.source_refs),
                )
            )
    for dxf in unmatched_dxf:
        output_id = output_ids_by_dxf_id[dxf.placement_id]
        issues.append(
            LayoutIssue(
                issue_id=f"layout-missing-report-{output_id}",
                severity="warning",
                kind="missing_in_report",
                placement_ids=[output_id],
                location=_location(dxf),
                message="图纸中识别到灯具符号，但报告位置明细中没有可靠对应项。",
                required_action="确认 PDF 报告是否覆盖同一房间和同一设计版本。",
                confidence="medium",
                source_refs=dxf.source_refs,
            )
        )
    for report_placement in unmatched_report:
        issues.append(
            LayoutIssue(
                issue_id=f"layout-missing-dxf-{report_placement.report_index or len(issues) + 1}",
                severity="warning",
                kind="missing_in_dxf",
                placement_ids=[
                    item.placement_id
                    for item in output
                    if item.report_index == report_placement.report_index
                ],
                location=_location(report_placement),
                message="报告有灯具位置，但 DXF 中没有可靠对应符号。",
                required_action="确认图纸是否缺少灯具图形、图层是否被过滤，或报告与图纸版本不一致。",
                confidence="high",
                source_refs=report_placement.source_refs,
            )
        )

    duplicate_groups = _duplicate_groups(dxf_placements)
    for group_number, group in enumerate(duplicate_groups, start=1):
        ids = [output_ids_by_dxf_id[item_in.placement_id] for item_in in group]
        issues.append(
            LayoutIssue(
                issue_id=f"layout-duplicate-{group_number:03d}",
                severity="warning",
                kind="duplicate",
                placement_ids=ids[:20],
                location=_location(group[0]),
                message="多个 DXF 灯具候选中心几乎重合，可能是重复图形或同一灯具被拆分识别。",
                required_action="回到原始图纸确认符号数量和实体组成。",
                confidence="medium",
                source_refs=_merge_refs(*(item.source_refs for item in group)),
            )
        )

    report_model_counts = {item.model: item.quantity or 0 for item in report.luminaires}
    model_parameters = {
        item.model: {
            "manufacturer": item.manufacturer,
            "product_code": item.product_code,
            "quantity": item.quantity,
            "power_w": item.power_w,
            "luminous_flux_lm": item.luminous_flux_lm,
            "source_refs": item.source_refs,
        }
        for item in report.luminaires
    }
    dxf_model_index_counts = Counter(item.dxf_model_index for item in dxf_placements if item.dxf_model_index)
    model_index_to_model = {
        index: model for model, index in ((model, _report_model_index(model, report_placements)) for model in report_model_counts)
        if index is not None
    }
    dxf_model_counts = {
        model_index_to_model.get(index, f"index:{index}"): count
        for index, count in dxf_model_index_counts.items()
    }
    checks = _checks(
        dxf_placements,
        report,
        output,
        dxf_model_counts,
        report_model_counts,
        coordinate_tolerance_m,
        transform,
        residuals,
        floor_plan.asset.source_name,
        report.source_name,
    )
    warnings = list(report.warnings)
    if len(pairs) < 3 and pairs:
        warnings.append("有效匹配少于 3 个锚点，未拟合二维相似变换；坐标一致性只能作有限判断。")
    if not pairs:
        warnings.append("DXF 与 PDF 没有形成可靠位置匹配，未生成坐标变换。")
    return LayoutAnalysis(
        project_revision=0,
        cad_source_name=floor_plan.asset.source_name,
        cad_sha256=floor_plan.asset.sha256,
        report_source_name=report.source_name,
        report_sha256=report.sha256,
        report_page_count=report.page_count,
        coordinate_transform=transform,
        dxf_count=len(dxf_placements),
        report_count=len(report_placements),
        model_counts=report_model_counts,
        model_parameters=model_parameters,
        placements=output,
        checks=checks,
        issues=issues,
        warnings=list(dict.fromkeys(warnings)),
    )


def _match_placements(
    dxf: list[LuminairePlacement], report: list[LuminairePlacement]
) -> tuple[list[_Pair], list[LuminairePlacement], list[LuminairePlacement]]:
    has_dxf_indices = any(item.dxf_model_index for item in dxf)
    groups: dict[str, tuple[list[LuminairePlacement], list[LuminairePlacement]]] = {}
    for item in dxf:
        key = item.dxf_model_index if has_dxf_indices else "all"
        groups.setdefault(key or "all", ([], []))[0].append(item)
    for item in report:
        eligible_key = item.dxf_model_index if has_dxf_indices else "all"
        groups.setdefault(eligible_key or "all", ([], []))[1].append(item)
    pairs: list[_Pair] = []
    paired_dxf_ids: set[str] = set()
    paired_report_ids: set[str] = set()
    for dxf_group, report_group in groups.values():
        for dxf_item, report_item in _match_group(dxf_group, report_group):
            pairs.append(_Pair(dxf=dxf_item, report=report_item))
            paired_dxf_ids.add(dxf_item.placement_id)
            paired_report_ids.add(report_item.placement_id)
    remaining = [item for item in dxf if item.placement_id not in paired_dxf_ids]
    return pairs, remaining, [item for item in report if item.placement_id not in paired_report_ids]


def _match_group(
    dxf: list[LuminairePlacement], report: list[LuminairePlacement]
) -> list[tuple[LuminairePlacement, LuminairePlacement]]:
    """Choose the lower-residual of stable-order and spatial-order matches."""

    if not dxf or not report:
        return []
    if len(dxf) == 1 or len(report) == 1:
        return [(dxf[0], report[0])]
    candidates = [
        list(zip(dxf, report, strict=False)),
        list(
            zip(
                sorted(dxf, key=lambda item: (item.y_m, item.x_m)),
                sorted(report, key=lambda item: (item.y_m, item.x_m)),
                strict=False,
            )
        ),
    ]
    return min(candidates, key=_matching_score)


def _matching_score(candidate: list[tuple[LuminairePlacement, LuminairePlacement]]) -> float:
    pairs = [_Pair(dxf=dxf, report=report) for dxf, report in candidate]
    transform = _fit_transform(pairs, "m") if len(pairs) >= 3 else None
    return sum(_residual(transform, pair.dxf, pair.report) for pair in pairs)


def _fit_transform(pairs: list[_Pair], source_units: str) -> SimilarityTransform | None:
    if len(pairs) < 3:
        return None
    source = [(pair.dxf.x_m, pair.dxf.y_m) for pair in pairs]
    target = [(pair.report.x_m, pair.report.y_m) for pair in pairs]
    source_center = _center(source)
    target_center = _center(target)
    a = sum(
        (sx - source_center[0]) * (tx - target_center[0])
        + (sy - source_center[1]) * (ty - target_center[1])
        for (sx, sy), (tx, ty) in zip(source, target, strict=True)
    )
    b = sum(
        (sx - source_center[0]) * (ty - target_center[1])
        - (sy - source_center[1]) * (tx - target_center[0])
        for (sx, sy), (tx, ty) in zip(source, target, strict=True)
    )
    denominator = sum((sx - source_center[0]) ** 2 + (sy - source_center[1]) ** 2 for sx, sy in source)
    if denominator <= 1e-12:
        return None
    scale = math.hypot(a, b) / denominator
    rotation = math.degrees(math.atan2(b, a))
    radians = math.radians(rotation)
    translation_x = target_center[0] - scale * (math.cos(radians) * source_center[0] - math.sin(radians) * source_center[1])
    translation_y = target_center[1] - scale * (math.sin(radians) * source_center[0] + math.cos(radians) * source_center[1])
    transformed = [_transform_point(point, scale, rotation, translation_x, translation_y) for point in source]
    residuals = [math.hypot(x - tx, y - ty) for (x, y), (tx, ty) in zip(transformed, target, strict=True)]
    return SimilarityTransform(
        scale=scale,
        rotation_deg=rotation,
        translation_x_m=translation_x,
        translation_y_m=translation_y,
        source_units=source_units,
        target_units="m",
        anchor_count=len(pairs),
        rms_residual_m=math.sqrt(sum(value * value for value in residuals) / len(residuals)),
        max_residual_m=max(residuals),
    )


def _checks(
    dxf: list[LuminairePlacement],
    report: LuminaireReport,
    output: list[LuminairePlacement],
    dxf_model_counts: dict[str, int],
    report_model_counts: dict[str, int],
    tolerance: float,
    transform: SimilarityTransform | None,
    residuals: list[float],
    cad_source_name: str,
    report_source_name: str,
) -> list[LayoutCheck]:
    refs = [f"{cad_source_name}:DLX_LUM", f"{report_source_name}:position-pages"]
    checks = [
        LayoutCheck(
            metric="luminaire_count",
            status="pass" if len(dxf) == len(report.placements) else "fail",
            observed=len(dxf),
            threshold=len(report.placements),
            unit="count",
            explanation=("DXF 灯具候选数量与 PDF 位置明细一致。" if len(dxf) == len(report.placements) else "DXF 灯具候选数量与 PDF 位置明细不一致。"),
            source_refs=refs,
        ),
        LayoutCheck(
            metric="model_count_consistency",
            status="pass" if dxf_model_counts == report_model_counts else "warning",
            observed=sum(dxf_model_counts.values()),
            threshold=sum(report_model_counts.values()),
            unit="count",
            explanation=("按型号索引统计的数量一致。" if dxf_model_counts == report_model_counts else "存在型号索引数量差异，需结合灯具索引和清单复核。"),
            source_refs=refs,
        ),
        LayoutCheck(
            metric="coordinate_residual",
            status=("pass" if residuals and max(residuals) <= tolerance else "warning" if residuals else "insufficient_data"),
            observed=max(residuals) if residuals else None,
            threshold=tolerance,
            unit="m",
            placement_ids=[item.placement_id for item in output if item.coordinate_residual_m is not None],
            explanation=("匹配坐标的最大拟合残差在配置容差内。" if residuals and max(residuals) <= tolerance else "匹配坐标存在超过容差的残差，不能直接视为同一位置。" if residuals else "没有足够的匹配坐标用于残差检查。"),
            source_refs=refs,
        ),
    ]
    if transform is None and len(dxf) >= 3 and len(report.placements) >= 3:
        checks.append(
            LayoutCheck(
                metric="coordinate_transform",
                status="warning",
                observed=None,
                threshold=3,
                unit="anchors",
                explanation="匹配点退化或无法拟合二维相似变换，坐标统一需要人工确认。",
                source_refs=refs,
            )
        )
    return checks


def _distance(left: LuminairePlacement, right: LuminairePlacement) -> float:
    return math.hypot(left.x_m - right.x_m, left.y_m - right.y_m)


def _residual(transform: SimilarityTransform | None, left: LuminairePlacement, right: LuminairePlacement) -> float:
    if transform is None:
        return _distance(left, right)
    x, y = _transform_point(
        (left.x_m, left.y_m),
        transform.scale,
        transform.rotation_deg,
        transform.translation_x_m,
        transform.translation_y_m,
    )
    return math.hypot(x - right.x_m, y - right.y_m)


def _transform_point(point: tuple[float, float], scale: float, rotation: float, tx: float, ty: float) -> tuple[float, float]:
    radians = math.radians(rotation)
    x, y = point
    return (
        scale * (math.cos(radians) * x - math.sin(radians) * y) + tx,
        scale * (math.sin(radians) * x + math.cos(radians) * y) + ty,
    )


def _center(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))


def _report_model_index(model: str, placements: list[LuminairePlacement]) -> str | None:
    for item in placements:
        if item.model == model:
            return item.dxf_model_index
    return None


def _location(item: LuminairePlacement):
    from ..schemas import CadPoint

    return CadPoint(x=item.x_m, y=item.y_m)


def _source_refs(item: LuminairePlacement) -> list[str]:
    return list(dict.fromkeys(item.source_refs))


def _merge_refs(*groups: list[str]) -> list[str]:
    return list(dict.fromkeys(reference for group in groups for reference in group))


def _duplicate_groups(placements: list[LuminairePlacement]) -> list[list[LuminairePlacement]]:
    groups: list[list[LuminairePlacement]] = []
    remaining = list(placements)
    while remaining:
        first = remaining.pop(0)
        group = [first]
        for item in remaining[:]:
            if _distance(first, item) <= 0.01:
                group.append(item)
                remaining.remove(item)
        if len(group) > 1:
            groups.append(group)
    return groups
