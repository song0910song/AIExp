"""Deterministic point-by-point illuminance field calculations for redesign."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from .photometry import PhotometryDistribution, parse_photometry_file
from .preview import _IntensityInterpolator


@dataclass(slots=True)
class FixtureKind:
    label: str
    distribution: PhotometryDistribution
    flux_lm: float
    maintenance_factor: float
    source_path: str
    integrated_flux_lm: float = field(init=False)
    reference_flux_lm: float = field(init=False)
    table_scale: float = field(init=False)
    _interpolator: _IntensityInterpolator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.flux_lm = float(self.flux_lm)
        self.maintenance_factor = float(self.maintenance_factor)
        self._interpolator = _IntensityInterpolator(self.distribution)
        self.integrated_flux_lm = self.distribution.total_flux_lm()
        reference = self.integrated_flux_lm if self.distribution.absolute_flux_declared else 1000.0
        self.reference_flux_lm = reference
        self.table_scale = self.flux_lm * self.maintenance_factor / max(reference, 1e-9)

    def candela(self, gamma_deg: float, azimuth_deg: float) -> float:
        return self._interpolator.candela_at(gamma_deg, azimuth_deg) * self.table_scale


@dataclass(frozen=True, slots=True)
class Fixture:
    x: float
    y: float
    height_m: float
    kind: FixtureKind


def load_fixture_kind(
    path: Path,
    label: str,
    flux_lm: float | None,
    maintenance_factor: float,
) -> FixtureKind:
    suffix = path.suffix.casefold().lstrip(".")
    if suffix not in {"ies", "ldt"}:
        raise ValueError(f"暂不支持 {path.suffix!r} 配光文件（仅支持 .ies / .ldt）")
    distribution = parse_photometry_file(path, suffix)  # type: ignore[arg-type]
    # For absolute photometry the integrated table is authoritative. For
    # relative files the declared lamp flux is authoritative when available.
    authoritative_flux = (
        distribution.total_flux_lm()
        if distribution.absolute_flux_declared and distribution.declared_flux_lm is None
        else distribution.declared_flux_lm
    )
    resolved_flux = authoritative_flux or flux_lm
    if resolved_flux is None or resolved_flux <= 0:
        raise ValueError("配光文件未声明光通量，必须显式提供 flux_lm")
    return FixtureKind(
        label=label,
        distribution=distribution,
        flux_lm=resolved_flux,
        maintenance_factor=maintenance_factor,
        source_path=str(path),
    )


def make_fixture(
    x: float,
    y: float,
    mounting_height_m: float,
    workplane_height_m: float,
    kind: FixtureKind,
) -> Fixture:
    height = float(mounting_height_m) - float(workplane_height_m)
    if height <= 0.05:
        raise ValueError("灯具安装高度必须至少高于工作面 0.05 m")
    return Fixture(float(x), float(y), height, kind)


def evaluate(points: Iterable[Iterable[float]], fixtures: Iterable[Fixture]) -> np.ndarray:
    """Evaluate horizontal illuminance at arbitrary work-plane points."""

    pts = np.asarray(list(points), dtype=float)
    if pts.size == 0:
        return np.zeros(0, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2:
        raise ValueError("评价点必须是 [x, y] 坐标数组")
    result = np.zeros(len(pts), dtype=float)
    for fixture in fixtures:
        dx = fixture.x - pts[:, 0]
        dy = fixture.y - pts[:, 1]
        horizontal = np.hypot(dx, dy)
        distance_sq = horizontal**2 + fixture.height_m**2
        gamma = np.degrees(np.arctan2(horizontal, fixture.height_m))
        azimuth = np.degrees(np.arctan2(dy, dx)) % 360.0
        cos_theta = fixture.height_m / np.sqrt(distance_sq)
        intensity = np.fromiter(
            (
                fixture.kind.candela(float(gamma[index]), float(azimuth[index]))
                for index in range(len(pts))
            ),
            dtype=float,
            count=len(pts),
        )
        result += intensity * cos_theta / distance_sq
    return result


def summarize(values: Iterable[float], watts: float, area_m2: float) -> dict[str, float]:
    array = np.asarray(list(values), dtype=float)
    if array.size == 0:
        raise ValueError("无法汇总空照度场")
    average = float(array.mean())
    minimum = float(array.min())
    maximum = float(array.max())
    return {
        "Em": round(average, 2),
        "Emin": round(minimum, 2),
        "Emax": round(maximum, 2),
        "Uo": round(minimum / average if average else 0.0, 4),
        "Ud": round(minimum / maximum if maximum else 0.0, 4),
        "watts": round(float(watts), 2),
        "LPD": round(float(watts) / area_m2 if area_m2 > 0 else 0.0, 3),
    }


def calibrate(reported_average_lx: float | None, raw_values: Iterable[float]) -> tuple[float, str | None]:
    raw = np.asarray(list(raw_values), dtype=float)
    if reported_average_lx is None:
        return 1.0, "DIALux 报告缺少平均照度，室内增益校准系数回退为 K=1.0。"
    mean = float(raw.mean()) if raw.size else 0.0
    if mean <= 0:
        raise ValueError("现状照度场均值为 0，无法标定校准系数")
    return float(reported_average_lx) / mean, None


__all__ = [
    "Fixture",
    "FixtureKind",
    "calibrate",
    "evaluate",
    "load_fixture_kind",
    "make_fixture",
    "summarize",
]
