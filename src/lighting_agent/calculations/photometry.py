"""Deterministic IES LM-63 and EULUMDAT (LDT) photometry parsing.

The parsed candela table feeds the approximate work-plane illuminance
preview. Vendor files stay untrusted input: every structural anomaly raises
:class:`PhotometryParseError` instead of being silently repaired.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Literal

from pydantic import Field, model_validator

from ..schemas import StrictModel

# Guards so a hostile file cannot allocate unbounded tables.
_MAX_CANDATA_CELLS = 400_000
_MAX_ANGLE_SAMPLES = 721

PhotometryFormat = Literal["ies", "ldt"]
PhotometricSystem = Literal["C", "B", "A"]


class PhotometryParseError(ValueError):
    """Raised when a photometric file cannot be parsed deterministically."""


class PhotometryDistribution(StrictModel):
    """One parsed luminous-intensity table in absolute candelas."""

    format: PhotometryFormat
    photometric_system: PhotometricSystem
    c_angles_deg: list[float] = Field(min_length=1)
    gamma_angles_deg: list[float] = Field(min_length=2)
    intensity_cd: list[list[float]]
    lamp_count: int = Field(default=1, ge=1)
    declared_flux_lm: float | None = Field(default=None, ge=0)
    # False means the candela table describes a 1000 lm reference lamp.
    absolute_flux_declared: bool = True
    power_w: float | None = Field(default=None, ge=0)
    units_type: Literal["meters", "feet", "unknown"] = "unknown"
    symmetry_note: str | None = None
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def tables_must_align(self) -> "PhotometryDistribution":
        if len(self.intensity_cd) != len(self.c_angles_deg):
            raise ValueError("one candela row per C angle is required")
        expected = len(self.gamma_angles_deg)
        unique_gamma = sorted(set(self.gamma_angles_deg))
        if len(unique_gamma) != expected:
            raise ValueError("vertical angles must be unique")
        if unique_gamma[0] != 0 or unique_gamma[-1] > 180:
            raise ValueError("vertical angles must start at 0 and end at or before 180 degrees")
        for row in self.intensity_cd:
            if len(row) != expected:
                raise ValueError("each candela row must match the vertical-angle count")
            if any(value < 0 or not math.isfinite(value) for value in row):
                raise ValueError("candela values must be finite and non-negative")
        if any(not math.isfinite(value) for value in self.c_angles_deg):
            raise ValueError("C angles must be finite")
        return self

    def angular_coverage_full_circle(self) -> bool:
        """True when the C angles describe a closed revolution around the axis."""

        return _covers_circle(sorted(set(self.c_angles_deg)))

    def total_flux_lm(self) -> float:
        """Integrate the table over the sphere (Type C orientation assumed)."""

        return _integrate_sphere_flux(
            self.c_angles_deg,
            self.gamma_angles_deg,
            self.intensity_cd,
        )

    def is_symmetric_about_vertical_axis(self) -> bool:
        rows = self.intensity_cd
        if len(rows) < 2:
            return True
        reference = rows[0]
        tolerance = max(reference) * 0.02 if max(reference) > 0 else 1e-9
        return all(
            all(abs(row[j] - reference[j]) <= tolerance for j in range(len(reference)))
            for row in rows[1:]
        )

    def summary(self) -> dict[str, object]:
        flat = [value for row in self.intensity_cd for value in row]
        peak_index = max(range(len(flat)), key=lambda index: flat[index])
        return {
            "format": self.format,
            "photometric_system": self.photometric_system,
            "lamp_count": self.lamp_count,
            "power_w": self.power_w,
            "declared_flux_lm": self.declared_flux_lm,
            "absolute_flux_declared": self.absolute_flux_declared,
            "integrated_flux_lm": round(self.total_flux_lm(), 1),
            "max_candela_cd": round(flat[peak_index], 1),
            "c_plane_count": len(self.c_angles_deg),
            "gamma_angle_count": len(self.gamma_angles_deg),
            "full_circle_coverage": self.angular_coverage_full_circle(),
            "symmetric_about_vertical_axis": self.is_symmetric_about_vertical_axis(),
            "symmetry_note": self.symmetry_note,
            "warnings": list(self.warnings),
        }


def parse_photometry(text: str, file_format: PhotometryFormat) -> PhotometryDistribution:
    """Parse LM-63 ``.ies`` or EULUMDAT ``.ldt`` content into a candela table."""

    cleaned = text.lstrip("\ufeff")
    if file_format == "ies":
        return _parse_ies(cleaned)
    if file_format == "ldt":
        return _parse_ldt(cleaned)
    raise PhotometryParseError(f"Unsupported photometry format: {file_format!r}")


def parse_photometry_file(path: Path, file_format: PhotometryFormat) -> PhotometryDistribution:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    except OSError as error:
        raise PhotometryParseError(f"无法读取配光文件：{error}") from error
    return parse_photometry(text, file_format)


def _covers_circle(angles: list[float]) -> bool:
    if len(angles) == 1:
        return True
    span = angles[-1] - angles[0]
    expected_step = 360 / (len(angles) - 1) if len(angles) > 1 else 360
    wrapped_gap = 360 - span
    return wrapped_gap <= expected_step + 1e-9


def _integrate_sphere_flux(
    c_angles: list[float], gamma: list[float], table: list[list[float]]
) -> float:
    """Solid-angle integration with midpoint candela per azimuth sector."""

    angles = sorted(set(c_angles))
    if len(angles) == 1:
        sectors: list[tuple[int, int, float]] = [(0, 0, 2 * math.pi)]
    else:
        sectors = []
        covered_circles = _covers_circle(angles)
        steps = [*([angles[i + 1] - angles[i] for i in range(len(angles) - 1)])]
        if covered_circles:
            steps.append(360 - angles[-1] + angles[0])
            partner = list(range(1, len(angles))) + [0]
        else:
            partner = list(range(1, len(angles)))
        for index, width in enumerate(steps):
            neighbour = partner[index] if index < len(partner) else partner[-1]
            sectors.append((index, neighbour, math.radians(width)))

    flux = 0.0
    for left_plane, right_plane, azimuth_width in sectors:
        left_row = table[left_plane]
        right_row = table[right_plane]
        for j in range(len(gamma) - 1):
            gamma_low = math.radians(gamma[j])
            gamma_high = math.radians(gamma[j + 1])
            # Solid angle of the spherical quad between the two latitude rings.
            solid_angle = azimuth_width * (math.cos(gamma_low) - math.cos(gamma_high))
            if solid_angle <= 0:
                continue
            mean_intensity = 0.25 * (
                left_row[j] + left_row[j + 1] + right_row[j] + right_row[j + 1]
            )
            flux += mean_intensity * solid_angle
    return flux


def _numeric_tokens(lines: Iterable[str]) -> list[str]:
    tokens: list[str] = []
    for line in lines:
        tokens.extend(line.split())
    return tokens


def _to_float(token: str, context: str) -> float:
    try:
        value = float(token)
    except ValueError as error:
        raise PhotometryParseError(f"{context} is not numeric: {token!r}") from error
    if not math.isfinite(value):
        raise PhotometryParseError(f"{context} is not finite: {token!r}")
    return value


class _NumberStream:
    """Whitespace-token cursor shared by both parsers."""

    def __init__(self, lines: list[str], start: int = 0) -> None:
        self._tokens = _numeric_tokens(lines[start:])
        self._offset = 0

    def take_float(self, context: str) -> float:
        if self._offset >= len(self._tokens):
            raise PhotometryParseError(f"Unexpected end of file while reading {context}")
        value = _to_float(self._tokens[self._offset], context)
        self._offset += 1
        return value

    def take_int(self, context: str, minimum: int | None = None, maximum: int | None = None) -> int:
        value = self.take_float(context)
        rounded = int(round(value))
        if abs(value - rounded) > 1e-6:
            raise PhotometryParseError(f"{context} must be an integer, got {value:g}")
        if minimum is not None and rounded < minimum:
            raise PhotometryParseError(f"{context} must be at least {minimum}")
        if maximum is not None and rounded > maximum:
            raise PhotometryParseError(f"{context} must be at most {maximum}")
        return rounded

    def take_float_block(self, count: int, context: str) -> list[float]:
        return [self.take_float(context) for _ in range(count)]


def _parse_ies(text: str) -> PhotometryDistribution:
    lines = [line.strip() for line in text.splitlines()]
    # The numeric header is the first line with at least ten whitespace
    # separated numbers; everything before it (IESNA keyword, [TEST] tags,
    # blanks) is metadata.
    body_start = len(lines)
    header_line_index: int | None = None
    for index, line in enumerate(lines):
        tokens = line.split()
        if len(tokens) < 10:
            continue
        try:
            [float(token) for token in tokens]
        except ValueError:
            continue
        header_line_index = index
        break
    if header_line_index is None:
        raise PhotometryParseError("未找到 IES 数值头部行")
    body_start = header_line_index + 1

    tilt_source = next(
        (
            line
            for line in lines[:header_line_index]
            if line.upper().startswith("[TILT]") or line.upper().startswith("TILT")
        ),
        "",
    )
    if tilt_source and "=NONE" not in tilt_source.upper().replace(" ", ""):
        raise PhotometryParseError("带 TILT 数据的 IES 文件不受支持")

    stream = _NumberStream(lines, header_line_index)
    lamp_count = stream.take_int("number of lamps", minimum=1, maximum=64)
    declared_lumens = stream.take_float("lumens per lamp")
    if declared_lumens < 0:
        raise PhotometryParseError("lumens per lamp cannot be negative")
    multiplier = stream.take_float("candela multiplier")
    if multiplier <= 0:
        raise PhotometryParseError("candela multiplier must be positive")
    vertical_count = stream.take_int("vertical angle count", minimum=2, maximum=_MAX_ANGLE_SAMPLES)
    horizontal_count = stream.take_int("horizontal angle count", minimum=1, maximum=_MAX_ANGLE_SAMPLES)
    system_code = stream.take_int("photometric type", minimum=1, maximum=3)
    units_code = stream.take_int("units type", minimum=1, maximum=2)
    # Width, length, height fields of the LM-63 header line.
    stream.take_float_block(3, "luminaire dimensions")

    if horizontal_count * vertical_count > _MAX_CANDATA_CELLS:
        raise PhotometryParseError("IES candela table exceeds the supported size")
    if system_code != 1:
        raise PhotometryParseError(
            "当前仅支持 C-γ（photometric type 1）配光文件；请使用提供该格式的产品数据"
        )

    distribution = _read_ies_body(
        stream,
        lamp_count=lamp_count,
        declared_lumens=declared_lumens,
        multiplier=multiplier,
        vertical_count=vertical_count,
        horizontal_count=horizontal_count,
        units_code=units_code,
    )
    return distribution


def _read_ies_body(
    stream: _NumberStream,
    *,
    lamp_count: int,
    declared_lumens: float,
    multiplier: float,
    vertical_count: int,
    horizontal_count: int,
    units_code: int,
) -> PhotometryDistribution:
    """Read angles plus candela rows; try with and without trailing header extras.

    LM-63-1995/2002 exporters append three optional numbers (ballast factor,
    future use, input watts) right after the dimensions. Older files omit
    them. The remaining token count usually identifies the layout directly;
    otherwise both layouts are attempted so the angle table stays reliable.
    """

    remaining_tokens = len(stream._tokens) - stream._offset
    needed_tokens = vertical_count + horizontal_count + vertical_count * horizontal_count
    if remaining_tokens == needed_tokens:
        layouts = (0,)
    elif remaining_tokens == needed_tokens + 3:
        layouts = (3,)
    else:
        layouts = (3, 0)

    last_error: PhotometryParseError | None = None
    for consume_extras in layouts:
        probe = _NumberStream(stream._tokens[stream._offset:])
        wattage: float | None = None
        try:
            if consume_extras:
                ballast = probe.take_float("ballast factor")
                probe.take_float("future use")
                candidate = probe.take_float("input watts")
                plausible_extras = (ballast == -1 or 0.5 <= ballast <= 2) and (
                    candidate <= 5_000
                )
                if not plausible_extras:
                    raise PhotometryParseError("trailing header values are implausible")
                if ballast != -1:
                    wattage = candidate
            gamma = probe.take_float_block(vertical_count, "vertical angles")
            if any(gamma[i] >= gamma[i + 1] for i in range(len(gamma) - 1)):
                raise PhotometryParseError("vertical angles must ascend")
            c_angles = probe.take_float_block(horizontal_count, "horizontal angles")
            if any(c_angles[i] >= c_angles[i + 1] for i in range(len(c_angles) - 1)):
                raise PhotometryParseError("horizontal angles must ascend")
            table: list[list[float]] = [
                probe.take_float_block(vertical_count, "candela values")
                for _ in range(horizontal_count)
            ]
        except PhotometryParseError as error:
            last_error = error
            continue

        applied_multiplier = multiplier
        if any(value < 0 for row in table for value in row):
            raise PhotometryParseError("candela values cannot be negative")

        absolute_flux = declared_lumens > 0
        warnings: list[str] = []
        if not absolute_flux:
            warnings.append(
                "文件未标注光源光通量，光强按 1000 lm 基准解析；预览时必须另行提供实际光通量。"
            )
        if probe._offset != len(probe._tokens):
            warnings.append("IES 文件末尾包含未使用的附加字段，已忽略。")
        if wattage is not None and wattage == -1:
            wattage = None

        return PhotometryDistribution(
            format="ies",
            photometric_system="C",
            c_angles_deg=[round(angle, 3) for angle in c_angles],
            gamma_angles_deg=[round(angle, 3) for angle in gamma],
            intensity_cd=[[round(value * applied_multiplier, 6) for value in row] for row in table],
            lamp_count=lamp_count,
            declared_flux_lm=(declared_lumens * lamp_count) if absolute_flux else None,
            absolute_flux_declared=absolute_flux,
            power_w=wattage,
            units_type="feet" if units_code == 1 else "meters",
            warnings=warnings,
        )
    raise last_error or PhotometryParseError("IES 文件无法解析")


def _read_leading_int(stream: _NumberStream, context: str, minimum: int, maximum: int) -> int:
    return stream.take_int(context, minimum=minimum, maximum=maximum)


def _parse_ldt(text: str) -> PhotometryDistribution:
    raw_lines = [line.rstrip() for line in text.splitlines()]
    if len(raw_lines) < 14:
        raise PhotometryParseError("LDT 文件缺少必需的头部行")
    # Lines 0..2: manufacturer / light source / luminaire identification.
    lamp_name = raw_lines[1].strip()

    stream = _NumberStream(raw_lines, 3)
    arc_count = _read_leading_int(stream, "arc count", 1, 64)
    flux_entry_count = _read_leading_int(stream, "luminous flux entry count", 0, 8)
    declared_total_flux: float | None = None
    flux_entries: list[float] = []
    if flux_entry_count:
        flux_entries = [
            stream.take_float("luminous flux value") for _ in range(flux_entry_count)
        ]
        positive_entries = [value for value in flux_entries if value > 0]
        if positive_entries:
            declared_total_flux = positive_entries[0]
    # Line 6 measurement report flag and line 7 conversion factor are ignored.
    stream.take_float("measurement condition")
    stream.take_float("calculation factor")

    tilt_flag = _read_leading_int(stream, "tilt flag", 0, 2)
    if tilt_flag != 0:
        raise PhotometryParseError("带 TILT 数据的 LDT 文件不受支持")
    c_plane_count = _read_leading_int(stream, "C plane count", 1, _MAX_ANGLE_SAMPLES)
    c_spacing_deg = stream.take_float("C plane spacing")
    if c_spacing_deg <= 0 or c_spacing_deg > 180:
        raise PhotometryParseError("C plane spacing must be within (0, 180]")
    gamma_count = _read_leading_int(stream, "vertical angle count", 2, _MAX_ANGLE_SAMPLES)
    if c_plane_count * gamma_count > _MAX_CANDATA_CELLS:
        raise PhotometryParseError("LDT candela table exceeds the supported size")

    gamma = []
    for _ in range(gamma_count):
        gamma.append(stream.take_float("vertical angle"))
    if any(gamma[i] >= gamma[i + 1] for i in range(len(gamma) - 1)):
        raise PhotometryParseError("vertical angles must ascend")
    if gamma[0] != 0 or gamma[-1] > 180:
        raise PhotometryParseError("vertical angles must start at 0 and end at or before 180 degrees")

    ratios = stream.take_float_block(6, "flux code ratios")
    dff_percent = ratios[0]
    if not 0 <= dff_percent <= 100:
        raise PhotometryParseError("downward flux fraction must be within [0, 100]")

    extended_zone_pairs = _read_leading_int(stream, "extended downward zone count", 0, 64)
    if extended_zone_pairs:
        zone_start = _read_leading_int(stream, "extended zone start", 1, 99)
        stream.take_float_block(zone_start, "extended downward zone ratios")
        upper_pair = _read_leading_int(stream, "extended upward zone count", 0, 64)
        if upper_pair:
            upper_start = _read_leading_int(stream, "extended zone start", 1, 99)
            stream.take_float_block(upper_start, "extended upward zone ratios")

    table: list[list[float]] = []
    warnings: list[str] = []
    for plane_index in range(c_plane_count):
        conversion = stream.take_float("plane conversion factor")
        row = stream.take_float_block(gamma_count, "candela values")
        if conversion < 0:
            raise PhotometryParseError("plane conversion factor cannot be negative")
        if conversion > 0:
            row = [value * conversion / 1000.0 for value in row]
        table.append(row)

    # Conversion factors rescale towards the declared lamp flux; when no flux
    # is declared the parsed table stays on the 1000 lm reference lamp basis.
    declared_total_flux = (
        declared_total_flux if declared_total_flux is not None and declared_total_flux > 0 else None
    )
    absolute_flux = declared_total_flux is not None
    if not absolute_flux:
        warnings.append(
            "LDT 未给出实测光通量，光强按 1000 lm 基准解析；预览时必须另行提供实际光通量。"
        )

    c_angles: list[float] = [
        round(index * c_spacing_deg, 3) for index in range(max(2, c_plane_count))
    ]
    if c_plane_count == 1:
        c_angles = [0.0]
        warnings.append("LDT 仅含一个 C 平面，按旋转对称处理。")

    metadata_power = _extract_power_hint(lamp_name)

    distribution = PhotometryDistribution(
        format="ldt",
        photometric_system="C",
        c_angles_deg=c_angles,
        gamma_angles_deg=[round(angle, 3) for angle in gamma],
        intensity_cd=[[round(value, 6) for value in row] for row in table],
        lamp_count=arc_count,
        declared_flux_lm=declared_total_flux,
        absolute_flux_declared=absolute_flux,
        power_w=metadata_power,
        units_type="meters",
        symmetry_note=f"DFF {dff_percent:g}%",
        warnings=warnings,
    )
    return distribution


def _extract_power_hint(lamp_name: str) -> float | None:
    """Pull a plausible wattage hint such as ``2x36W`` from a lamp label."""

    import re

    match = re.search(r"(\d+(?:\.\d+)?)\s*[Ww]", lamp_name)
    if match is None:
        return None
    value = float(match.group(1))
    multiplier_match = re.match(r"\s*(\d+)\s*[xX×]", lamp_name)
    multiplier = int(multiplier_match.group(1)) if multiplier_match else 1
    if 0 < value <= 10_000 and 1 <= multiplier <= 10:
        return value * multiplier
    return None
