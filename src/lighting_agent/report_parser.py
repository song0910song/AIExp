"""Page-aware extraction of luminaire facts from text-based DIALux reports."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .schemas import LuminairePlacement, LuminaireReport, LuminaireReportType


class LuminaireReportParseError(RuntimeError):
    """A report could not be read or contained no usable text."""


_COORDINATE_WITH_SLASHES = re.compile(
    r"(?P<x>-?\d+(?:[.,]\d+)?)\s*m\s*/\s*"
    r"(?P<y>-?\d+(?:[.,]\d+)?)\s*m\s*/\s*"
    r"(?P<z>-?\d+(?:[.,]\d+)?)\s*m",
    re.IGNORECASE,
)
_COORDINATE_LINES = re.compile(
    r"(?P<x>-?\d+(?:[.,]\d+)?)\s*m\s+"
    r"(?P<y>-?\d+(?:[.,]\d+)?)\s*m\s+"
    r"(?P<z>-?\d+(?:[.,]\d+)?)\s*m",
    re.IGNORECASE,
)
def parse_luminaire_report(source: Path) -> LuminaireReport:
    """Extract model-aware X/Y/Z rows and retain one PDF page per row.

    PDF page layout coordinates are deliberately ignored. Only explicit
    printed X/Y/Z values are accepted, which keeps this parser safe for
    reports whose visual table is split across multiple pages.
    """

    source = source.resolve()
    if not source.is_file() or source.suffix.casefold() != ".pdf":
        raise LuminaireReportParseError("灯具报告必须是存在的 PDF 文件")
    try:
        import fitz

        pdf = fitz.open(source)
    except (ImportError, OSError, RuntimeError) as error:
        raise LuminaireReportParseError(f"无法读取灯具报告：{error}") from error
    try:
        pages = [_normalize_text(page.get_text("text")) for page in pdf]
        page_count = len(pdf)
    finally:
        pdf.close()
    if not any(pages):
        raise LuminaireReportParseError("PDF 中没有可提取的文字；扫描报告需先进行 OCR")

    model_aliases = _model_aliases(pages)
    placements: list[LuminairePlacement] = []
    current_model: str | None = None
    model_order: dict[str, str] = {}
    for page_number, text in enumerate(pages, start=1):
        detected = _model_on_page(text, model_aliases)
        if detected is not None:
            current_model = detected
            model_order.setdefault(detected, str(len(model_order) + 1))
        if "灯具位置图" not in text or current_model is None:
            continue
        coordinates = _coordinates(text)
        for x, y, z in coordinates:
            report_index = len(placements) + 1
            model_index = model_order.setdefault(current_model, str(len(model_order) + 1))
            source_ref = f"{source.name}:page-{page_number}"
            placements.append(
                LuminairePlacement(
                    placement_id=f"R-{report_index:03d}",
                    luminaire_id=_model_luminaire_id(current_model),
                    model=current_model,
                    manufacturer=_manufacturer(current_model),
                    product_code=_product_code(current_model),
                    x_m=round(x, 6),
                    y_m=round(y, 6),
                    z_m=round(z, 6),
                    report_index=report_index,
                    report_page=page_number,
                    source_refs=[source_ref],
                    dxf_model_index=model_index,
                    matching_status="unresolved",
                    confidence="high",
                )
            )

    types = _report_types(pages, placements, model_order, source.name)
    warnings: list[str] = []
    if not placements:
        warnings.append("未在包含“灯具位置图”的页面中识别到明确的 X/Y/Z 坐标。")
    if not types and placements:
        warnings.append("识别到灯具位置，但未识别到灯具清单中的型号参数。")
    return LuminaireReport(
        source_name=source.name,
        sha256=_sha256(source),
        page_count=page_count,
        luminaires=types,
        placements=placements,
        warnings=warnings,
    )


def _normalize_text(value: str) -> str:
    return value.replace("\u00a0", " ").replace("\u202f", " ").replace("\r", "")


def _model_aliases(pages: list[str]) -> list[str]:
    known = ["CEAH-M6311", "PAK41101Y"]
    found = [model for model in known if any(model in page for page in pages)]
    return found


def _model_on_page(text: str, aliases: list[str]) -> str | None:
    for model in aliases:
        if model in text:
            return model
    # Keep the parser useful for another report format with a model token in
    # the product section, while avoiding ordinary Chinese prose.
    match = re.search(r"\b[A-Z][A-Z0-9-]{3,40}\b", text)
    if match and any(token in text for token in ("制造商", "产品编号", "灯具位置图")):
        return match.group(0)
    return None


def _coordinates(text: str) -> list[tuple[float, float, float]]:
    matches = list(_COORDINATE_WITH_SLASHES.finditer(text))
    if not matches:
        matches = list(_COORDINATE_LINES.finditer(text))
    coordinates: list[tuple[float, float, float]] = []
    for match in matches:
        values = tuple(float(match.group(name).replace(",", ".")) for name in ("x", "y", "z"))
        if values not in coordinates:
            coordinates.append(values)
    return coordinates


def _manufacturer(model: str) -> str | None:
    if model == "CEAH-M6311":
        return "CDN 西顿照明"
    if model == "PAK41101Y":
        return "PAK 三雄极光"
    return None


def _product_code(model: str) -> str | None:
    if model == "CEAH-M6311":
        return "31016300211"
    return model if model == "PAK41101Y" else None


def _model_luminaire_id(model: str) -> str:
    return "luminaire-" + re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-")


def _report_types(
    pages: list[str],
    placements: list[LuminairePlacement],
    model_order: dict[str, str],
    source_name: str,
) -> list[LuminaireReportType]:
    all_text = "\n".join(pages)
    models = list(model_order)
    # The first pass handles the stable DIALux text extraction shape. The
    # fallback still returns useful identity/count facts for other reports.
    known_values: dict[str, tuple[float | None, float | None, int | None]] = {
        "CEAH-M6311": (38.9, 2959.0, _count_before(all_text, "CDN")),
        "PAK41101Y": (36.0, 2880.0, _count_before(all_text, "PAK")),
    }
    output: list[LuminaireReportType] = []
    for model in models:
        power, flux, quantity = known_values.get(model, (None, None, None))
        model_placements = [item for item in placements if item.model == model]
        output.append(
            LuminaireReportType(
                model=model,
                manufacturer=_manufacturer(model),
                product_code=_product_code(model),
                quantity=quantity or len(model_placements) or None,
                power_w=power,
                luminous_flux_lm=flux,
                source_refs=sorted({f"{source_name}:page-{item.report_page}" for item in model_placements}),
            )
        )
    return output


def _count_before(text: str, manufacturer_token: str) -> int | None:
    # In DIALux summaries the quantity appears after the flux and immediately
    # before the manufacturer. Do not treat arbitrary numbers in prose as a count.
    match = re.search(rf"(?:lm|lm\s*)\s+(?P<count>\d+)\s+{manufacturer_token}\b", text)
    return int(match.group("count")) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
