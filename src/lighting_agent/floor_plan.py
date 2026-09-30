"""Safe, bounded extraction of 2D lighting-design context from CAD drawings."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import string
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Literal, cast

import ezdxf
from ezdxf import bbox
from ezdxf.addons import odafc
from ezdxf.document import Drawing
from ezdxf.filemanagement import readfile
from ezdxf.lldxf.const import DXFError

from .schemas import (
    CadPoint,
    FloorPlan,
    FloorPlanAsset,
)
from .cad_geometry import inspect_drawing
from .spatial_model import build_spatial_model


class FloorPlanParseError(RuntimeError):
    """A drawing could not be safely converted or parsed."""


MAX_TEXT_ITEMS = 200
MAX_DRAWING_BYTES = 50 * 1024 * 1024
SUPPORTED_DRAWING_SUFFIXES = frozenset({".dxf", ".dwg"})
ODA_FILE_CONVERTER_ENV_VAR = "ODA_FILE_CONVERTER_PATH"
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
    if source.stat().st_size > MAX_DRAWING_BYTES:
        raise FloorPlanParseError("CAD 文件不能超过 50 MB")
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
    # DIALux exports can carry an inch unit header while DLX_* coordinates are
    # already metric.  Normalize the effective scale before persisting facts.
    dialux_metric = _uses_dialux_metric_coordinates(entities)
    effective_meters_per_unit = 1.0 if dialux_metric else meters_per_unit
    inspection = inspect_drawing(document, effective_meters_per_unit)
    area_candidates = inspection.pop("area_candidates")
    elements = inspection.pop("elements")
    room_name = _room_name(text_items)
    if not effective_meters_per_unit:
        warnings.append("图纸未声明可换算的长度单位；面积与尺寸仅能作为原始单位参考。")
    if not area_candidates:
        warnings.append("未识别到闭合房间边界；请在图纸中使用闭合多段线或在界面手动填写面积。")

    normalized_units = "m" if dialux_metric else unit_name
    plan = FloorPlan(
        asset=FloorPlanAsset(
            source_name=source.name,
            source_type=cast(Literal["dxf", "dwg"], suffix[1:]),
            storage_path=storage_path,
            sha256=_sha256(source),
            size_bytes=source.stat().st_size,
            converted_from_dwg=converted_from_dwg,
        ),
        drawing_units=normalized_units,
        meters_per_drawing_unit=effective_meters_per_unit,
        bounds=bounds,
        entity_counts=dict(sorted(entity_counts.items())),
        text_items=text_items,
        room_name=room_name,
        area_candidates=area_candidates,
        warnings=list(dict.fromkeys(warnings)),
        conversion_log=[
            f"原文件 SHA-256: {_sha256(source)}",
            "ODA 本机临时 DXF 转换完成" if converted_from_dwg else "直接读取 DXF（不改写原文件）",
            f"DXF 版本: {document.dxfversion}; 声明单位: {unit_name}",
            *(["识别到 DIALux DLX 图层，按米解释坐标；请核对尺度"] if dialux_metric else []),
        ],
        **inspection,
    )
    plan.spatial_model = build_spatial_model(plan, elements)
    return plan


def _read_document(source: Path) -> tuple[Drawing, bool, list[str]]:
    if source.suffix.casefold() == ".dxf":
        return readfile(source), False, []
    converter = _configure_oda_file_converter()
    if converter is None:
        raise FloorPlanParseError(
            "未找到可用的 ODA File Converter，无法安全转换 DWG。"
            f"请安装转换器，或通过 {ODA_FILE_CONVERTER_ENV_VAR} 配置 ODAFileConverter.exe 的完整路径。"
        )
    with tempfile.TemporaryDirectory(prefix="lighting-dwg-") as temporary_directory:
        converted = Path(temporary_directory) / f"{source.stem}.dxf"
        odafc.convert(source, converted, replace=True)
        return readfile(converted), True, [
            "DWG 已在本机转换为临时 DXF 后解析；原始 DWG 未被修改。",
            f"转换器: {converter}; ezdxf: {ezdxf.__version__}; 转换 DXF SHA-256: {_sha256(converted)}",
        ]


def _configure_oda_file_converter() -> Path | None:
    """Find ODA File Converter and configure ezdxf for custom installs.

    ``ezdxf`` only checks its configured Windows path in ``is_installed()``;
    it does not consult PATH and its default does not cover versioned or custom
    installation directories. Resolve those locations before calling odafc.
    """

    converter = _find_oda_file_converter()
    if converter is None:
        return None
    option_name = "win_exec_path" if os.name == "nt" else "unix_exec_path"
    ezdxf.options.set("odafc-addon", option_name, str(converter))
    return converter


def _find_oda_file_converter() -> Path | None:
    candidates: list[Path] = []

    configured = odafc.get_win_exec_path()
    if configured:
        candidates.append(Path(configured))

    explicit = os.getenv(ODA_FILE_CONVERTER_ENV_VAR, "").strip().strip('"')
    if explicit:
        candidates.insert(0, Path(explicit).expanduser())

    on_path = shutil.which("ODAFileConverter")
    if on_path:
        candidates.append(Path(on_path))

    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            value = os.getenv(variable)
            if not value:
                continue
            oda_root = Path(value) / "ODA"
            candidates.append(oda_root / "ODAFileConverter" / "ODAFileConverter.exe")
            if oda_root.is_dir():
                candidates.extend(oda_root.glob("ODAFileConverter*/ODAFileConverter.exe"))

        # The official MSI permits a custom target such as F:\\oda. Checking
        # this single conventional directory on each drive is bounded and
        # avoids an expensive recursive filesystem search.
        candidates.extend(
            Path(f"{letter}:\\oda\\ODAFileConverter.exe")
            for letter in string.ascii_uppercase
        )

    seen: set[Path] = set()
    for candidate in candidates:
        normalized = candidate.resolve(strict=False)
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized.is_file():
            return normalized
    return None


def _drawing_unit(document: Drawing) -> tuple[str, float | None]:
    unit_code = int(document.header.get("$INSUNITS", 0) or 0)
    return _UNIT_TO_METERS.get(unit_code, (f"unknown:{unit_code}", None))


def _uses_dialux_metric_coordinates(entities: list[Any]) -> bool:
    """Detect DIALux exports whose DLX coordinates are already metres."""

    layers = {str(entity.dxf.layer).upper() for entity in entities}
    return "DLX_CONT" in layers and bool(
        layers & {"DLX_LUM", "DLX_OBJ", "DLX_APERT", "DLX_CALC"}
    )


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
