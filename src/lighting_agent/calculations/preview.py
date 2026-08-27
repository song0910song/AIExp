"""Approximate work-plane illuminance preview from a parsed candela table.

This is deliberately *not* a lighting simulation: it combines the direct
point-by-point component with an optional uniform inter-reflection term so
the average can match the confirmed lumen-method result. Every output is
labelled as an approximation and must never be reported as a DIALux-quality
compliance conclusion.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, model_validator

from ..schemas import StrictModel
from .photometry import PhotometryDistribution, PhotometryParseError

SOLVER_VERSION = "illuminance-preview-1"

_PREVIEW_LIMITATIONS = [
    "预览不包含家具遮挡、精确反射比、非均匀相互反射与可靠 UGR 计算。",
    "网格与布灯为系统生成的规则近似，不代表最终施工图布置。",
    "该结果仅为趋势参考；合规结论仍必须在 DIALux evo 或等效软件中核验。",
]


class PreviewGeometryError(ValueError):
    """Raised when preview inputs cannot form a solvable geometry."""


class IlluminancePreviewRequest(StrictModel):
    """Inputs for one approximate illuminance preview run."""

    luminaire_id: str = Field(min_length=1, max_length=128)
    lighting_group_id: str | None = Field(default=None, min_length=8, max_length=64)
    fixture_rows: int = Field(default=2, ge=1, le=24)
    fixture_columns: int = Field(default=3, ge=1, le=24)
    room_length_m: float | None = Field(default=None, gt=0, le=1_000)
    room_width_m: float | None = Field(default=None, gt=0, le=1_000)
    workplane_height_m: float | None = Field(default=None, ge=0, le=10)
    mounting_height_m: float | None = Field(default=None, gt=0, le=100)
    maintenance_factor: float | None = Field(default=None, gt=0, le=1)
    utilization_factor: float | None = Field(default=None, gt=0, le=1)
    total_flux_lm: float | None = Field(default=None, gt=0, le=10_000_000)


class IlluminancePreviewResult(StrictModel):
    """Grid values and summary statistics for one preview computation."""

    solver_version: Literal["illuminance-preview-1"] = SOLVER_VERSION
    inputs: IlluminancePreviewRequest
    fixture_positions_m: list[list[float]]
    grid_rows: int = Field(ge=1)
    grid_columns: int = Field(ge=1)
    grid_x_coordinates_m: list[float]
    grid_y_coordinates_m: list[float]
    illuminance_lx: list[list[float]]
    direct_average_lx: float
    calibration_scale: float
    average_illuminance_lx: float
    minimum_illuminance_lx: float
    maximum_illuminance_lx: float
    uniformity_u0: float
    installed_flux_lm: float
    installed_power_w: float | None = None
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def grid_must_align(self) -> "IlluminancePreviewResult":
        if len(self.illuminance_lx) != self.grid_rows or any(
            len(row) != self.grid_columns for row in self.illuminance_lx
        ):
            raise ValueError("illuminance rows must match the declared grid size")
        return self


def compute_illuminance_preview(
    distribution: PhotometryDistribution,
    request: IlluminancePreviewRequest,
) -> IlluminancePreviewResult:
    """Evaluate the point-by-point grid for the requested fixture array."""

    length = request.room_length_m
    width = request.room_width_m
    if not length or not width or length <= 0 or width <= 0:
        raise PreviewGeometryError("照度预览需要确认房间长度与宽度（米）")

    mounting_height = request.mounting_height_m
    if mounting_height is None:
        raise PreviewGeometryError("缺少吊装点高度（mounting_height_m）")
    workplane = request.workplane_height_m if request.workplane_height_m is not None else 0.75
    emission_height = mounting_height - workplane
    if emission_height <= 0.05:
        raise PreviewGeometryError(
            f"吊装点高度 {mounting_height:g} m 必须至少高于工作面 {workplane:g} m 0.05 m"
        )

    flux_per_fixture = (
        request.total_flux_lm
        if request.total_flux_lm is not None
        else distribution.declared_flux_lm
    )
    reference_flux = (
        distribution.total_flux_lm() if distribution.absolute_flux_declared else 1000.0
    )
    if flux_per_fixture is None or flux_per_fixture <= 0:
        raise PreviewGeometryError(
            "配光文件未提供光通量且未指定 total_flux_lm，无法换算实际照度"
        )
    table_scale = flux_per_fixture / max(reference_flux, 1e-9)
    maintenance = request.maintenance_factor if request.maintenance_factor is not None else 1.0
    table_scale *= maintenance

    warnings = list(distribution.warnings)
    if not distribution.angular_coverage_full_circle():
        warnings.append(
            "配光数据仅覆盖部分水平角，方位插值按最近平面处理；横向不对称趋势可能失真。"
        )

    fixtures = _fixture_positions(length, width, request.fixture_rows, request.fixture_columns)
    grid_columns, xs = _evaluation_axis(width)
    grid_rows, ys = _evaluation_axis(length)

    interp = _IntensityInterpolator(distribution)
    direct = [[0.0] * grid_columns for _ in range(grid_rows)]
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            point_value = 0.0
            for fx, fy in fixtures:
                dx = fx - x
                dy = fy - y
                horizontal = math.hypot(dx, dy)
                distance_sq = horizontal * horizontal + emission_height * emission_height
                gamma_deg = math.degrees(math.atan2(horizontal, emission_height))
                cos_theta = emission_height / math.sqrt(distance_sq)
                azimuth = math.degrees(math.atan2(dy, dx))
                intensity = interp.candela_at(gamma_deg, azimuth)
                point_value += intensity * table_scale * cos_theta / distance_sq
            direct[j][i] = point_value

    flat_direct = [value for row in direct for value in row]
    direct_average = sum(flat_direct) / len(flat_direct)

    calibration_scale = 1.0
    assumptions: list[str] = []
    if request.utilization_factor is not None:
        area = length * width
        target_average = (
            flux_per_fixture * len(fixtures) * request.utilization_factor * maintenance / area
        )
        if direct_average > 0:
            calibration_scale = target_average / direct_average
            assumptions.append(
                "空间分布形状由逐点法直接分量确定；整体水平按流明法（已确认利用系数）乘法校准。"
            )
    else:
        assumptions.append("未提供利用系数：结果仅包含灯具直射分量，平均照度偏低属预期。")
    assumptions.append(f"每个灯具光通量取 {flux_per_fixture:g} lm，维护系数取 {maintenance:g}。")

    final_rows = [
        [round(value * calibration_scale, 2) for value in row] for row in direct
    ]
    flat_final = [value for row in final_rows for value in row]
    average = sum(flat_final) / len(flat_final)
    minimum = min(flat_final)
    maximum = max(flat_final)

    per_fixture_power = None
    if distribution.power_w is not None and distribution.lamp_count > 0:
        per_fixture_power = round(distribution.power_w / distribution.lamp_count, 2)

    return IlluminancePreviewResult(
        inputs=request.model_copy(),
        fixture_positions_m=[[round(x, 3), round(y, 3)] for x, y in fixtures],
        grid_rows=grid_rows,
        grid_columns=grid_columns,
        grid_x_coordinates_m=[round(value, 3) for value in xs],
        grid_y_coordinates_m=[round(value, 3) for value in ys],
        illuminance_lx=final_rows,
        direct_average_lx=round(direct_average, 2),
        calibration_scale=round(calibration_scale, 4),
        average_illuminance_lx=round(average, 2),
        minimum_illuminance_lx=round(minimum, 2),
        maximum_illuminance_lx=round(maximum, 2),
        uniformity_u0=round(minimum / average, 4) if average > 0 else 0.0,
        installed_flux_lm=round(flux_per_fixture * len(fixtures), 1),
        installed_power_w=(
            round(per_fixture_power * len(fixtures), 2) if per_fixture_power is not None else None
        ),
        assumptions=assumptions,
        limitations=_PREVIEW_LIMITATIONS,
    )


def _fixture_positions(
    length: float, width: float, rows: int, columns: int
) -> list[tuple[float, float]]:
    """Evenly distributed fixture centres avoiding wall-adjacent placement."""

    positions: list[tuple[float, float]] = []
    for r in range(rows):
        y = (r + 0.5) * length / rows
        for c in range(columns):
            x = (c + 0.5) * width / columns
            positions.append((x, y))
    return positions


def _evaluation_axis(room_size: float) -> tuple[int, list[float]]:
    """Deterministic evaluation axis with roughly 8-21 sample points."""

    count = max(8, min(21, int(round(room_size * 3)) or 8))
    axis = [(index + 0.5) * room_size / count for index in range(count)]
    return count, axis


class _IntensityInterpolator:
    """Bilinear candela lookup across C planes with periodic wrap support."""

    def __init__(self, distribution: PhotometryDistribution) -> None:
        self.gamma_deg = list(distribution.gamma_angles_deg)
        self.table = distribution.intensity_cd
        self.planes = sorted(set(distribution.c_angles_deg))
        self.periodic = distribution.angular_coverage_full_circle()
        self.index_by_angle = {angle: index for index, angle in enumerate(self.planes)}

    def candela_at(self, gamma_deg: float, azimuth_deg: float) -> float:
        g = min(max(gamma_deg, 0.0), 180.0)
        low_index = self._gamma_index(g)
        high_index = min(low_index + 1, len(self.gamma_deg) - 1)
        span = self.gamma_deg[high_index] - self.gamma_deg[low_index]
        t = 0.0 if span <= 0 else (g - self.gamma_deg[low_index]) / span
        angle_a, angle_b, weight = self._azimuth_pair(azimuth_deg % 360.0)
        row_a = self.table[self.index_by_angle[angle_a]]
        row_b = self.table[self.index_by_angle[angle_b]]
        value_a = row_a[low_index] * (1 - t) + row_a[high_index] * t
        value_b = row_b[low_index] * (1 - t) + row_b[high_index] * t
        return value_a * (1 - weight) + value_b * weight

    def _gamma_index(self, gamma_deg: float) -> int:
        if gamma_deg <= self.gamma_deg[0]:
            return 0
        if gamma_deg >= self.gamma_deg[-1]:
            return len(self.gamma_deg) - 2
        low = 0
        high = len(self.gamma_deg) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if self.gamma_deg[middle] <= gamma_deg:
                low = middle
            else:
                high = middle
        return low

    def _azimuth_pair(self, azimuth: float) -> tuple[float, float, float]:
        planes = self.planes
        if len(planes) == 1:
            return planes[0], planes[0], 0.0
        if not self.periodic:
            nearest = min(planes, key=lambda angle: abs(angle - azimuth))
            return nearest, nearest, 0.0

        # Uniform full-circle sampling: locate the bracketing sector and
        # interpolate towards its neighbour, wrapping around 360 degrees.
        pitch = 360.0 / len(planes)
        offset = ((azimuth - planes[0]) % 360.0) / pitch
        lower_index = int(offset) % len(planes)
        upper_index = (lower_index + 1) % len(planes)
        weight = offset - int(offset)
        return planes[lower_index], planes[upper_index], weight


__all__ = [
    "SOLVER_VERSION",
    "IlluminancePreviewRequest",
    "IlluminancePreviewResult",
    "PreviewGeometryError",
    "PhotometryParseError",
    "compute_illuminance_preview",
]
