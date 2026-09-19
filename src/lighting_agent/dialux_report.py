"""Deterministic extraction of structured facts from DIALux PDF reports."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

MAX_REPORT_BYTES = 50 * 1024 * 1024

_COLUMN_RANGES = {
    "count": (40, 90),
    "brand": (90, 150),
    "code": (150, 192),
    "name": (192, 405),
    "rug": (405, 435),
    "power": (435, 478),
    "flux": (478, 520),
    "efficacy": (520, 570),
}


class DialuxReportParseError(ValueError):
    pass


def _rows(page, tolerance: float = 4.0) -> list[list[Any]]:
    buckets: dict[int, list[Any]] = {}
    for word in page.get_text("words"):
        buckets.setdefault(round(word[1] / tolerance), []).append(word)
    return [sorted(buckets[key], key=lambda item: (item[0], item[1])) for key in sorted(buckets)]


def _cell(words: list[Any], bounds: tuple[float, float]) -> str:
    left, right = bounds
    selected = [
        word for word in words if left <= (word[0] + word[2]) / 2 < right and word[1] < 780
    ]
    selected.sort(key=lambda item: (round(item[1] / 4), item[0]))
    return " ".join(str(word[4]) for word in selected).strip()


def _first_number(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group()) if match else None


def _luminaire_table(page, provenance: list[str]) -> list[dict[str, Any]]:
    rows = _rows(page)
    header = next(
        (
            index
            for index, row in enumerate(rows)
            if any(word[4] == "件数" for word in row) and any(word[4] == "品名" for word in row)
        ),
        None,
    )
    if header is None:
        return []
    provenance.append(f"page {page.number + 1}")
    has_rug = any(word[4] == "RUG" for word in rows[header])
    blocks: list[list[Any]] = []
    for row in rows[header + 1 :]:
        if re.fullmatch(r"\d+", _cell(row, _COLUMN_RANGES["count"])):
            blocks.append([])
        if blocks:
            blocks[-1].extend(row)
    result: list[dict[str, Any]] = []
    for words in blocks[:100]:
        count = _first_number(_cell(words, _COLUMN_RANGES["count"]))
        entry = {
            "count": int(count) if count is not None else None,
            "brand": _cell(words, _COLUMN_RANGES["brand"]) or None,
            "code": _cell(words, _COLUMN_RANGES["code"]).replace(" ", "") or None,
            "name": _cell(words, _COLUMN_RANGES["name"]) or None,
            "watts": _first_number(_cell(words, _COLUMN_RANGES["power"])),
            "flux_lm": _first_number(_cell(words, _COLUMN_RANGES["flux"])),
            "efficacy_lm_w": _first_number(_cell(words, _COLUMN_RANGES["efficacy"])),
        }
        if has_rug:
            rug = _first_number(_cell(words, _COLUMN_RANGES["rug"]))
            entry["rug"] = int(rug) if rug is not None else None
        result.append(entry)
    return result


def parse_dialux_report(path: Path) -> dict[str, Any]:
    """Extract the report summary, metrics, schedule and per-fixture positions."""

    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise DialuxReportParseError("DIALux PDF 报告不存在或为空")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise DialuxReportParseError("DIALux PDF 报告不能超过 50 MB")
    if path.read_bytes()[:5] != b"%PDF-":
        raise DialuxReportParseError("文件内容不是 PDF")
    try:
        import fitz

        document = fitz.open(path)
    except Exception as error:
        raise DialuxReportParseError(f"无法打开 DIALux PDF 报告：{error}") from error
    try:
        texts = [page.get_text("text") for page in document]
        if not any("DIALux" in text or "计算元件" in text for text in texts):
            raise DialuxReportParseError("PDF 未识别为可解析的 DIALux 设计报告")

        def search(pattern: str, value: str, group: int = 1) -> str | None:
            match = re.search(pattern, value, re.S)
            return match.group(group) if match else None

        def number(pattern: str, value: str, group: int = 1) -> float | None:
            found = search(pattern, value, group)
            return float(found.replace(",", "")) if found else None

        summary_page = texts[1] if len(texts) > 1 else ""
        results_page = texts[2] if len(texts) > 2 else ""
        surfaces = "\n".join(
            text for text in texts if "计算元件" in text or "工作面" in text
        )
        summary: dict[str, Any] = {
            "maintenance_factor": number(r"维护系数\s*\n\s*([\d.]+)", summary_page),
            "reflectance_pct": {
                "ceiling": number(r"天花板:\s*([\d.]+)", summary_page),
                "wall": number(r"墙壁:\s*([\d.]+)", summary_page),
                "floor": number(r"地板:\s*([\d.]+)", summary_page),
            },
            "floor_area_m2": number(r"地面\s*\n\s*([\d.]+)\s*m²", summary_page),
            "workplane_height_m": number(r"高度\s*工作面\s*\n\s*([\d.]+)\s*m", summary_page),
            "edge_zone_m": number(r"边缘区\s*工作面\s*\n\s*([\d.]+)\s*m", summary_page),
            "mounting_height_range_m": [],
        }
        mounting = re.search(r"安装高度\s*\n\s*([\d.]+)\s*m\s*–\s*([\d.]+)\s*m", summary_page)
        if mounting:
            summary["mounting_height_range_m"] = [float(mounting.group(1)), float(mounting.group(2))]
        results: dict[str, Any] = {
            "mean_lx": number(r"([\d.]+) lx\s*\n\s*\(≥\s*[\d.]+ lx\)", surfaces),
            "target_lx": number(r"\(≥\s*([\d.]+) lx\)", surfaces),
            "min_lx": number(r"\(≥\s*[\d.]+ lx\)\s*\n\s*([\d.]+) lx", surfaces),
            "max_lx": number(r"\(≥\s*[\d.]+ lx\)\s*\n\s*[\d.]+ lx\s*\n\s*([\d.]+) lx", surfaces),
            "uo": number(r"[\d.]+ lx\s*\n\s*([\d.]+)\s*\n\s*\(≥", surfaces),
            "uo_target": number(r"([\d.]+)\)\s*\n\s*[\d.]+\s*\n\s*WP1", surfaces),
            "g2": number(r"\(≥\s*[\d.]+\)\s*\n\s*([\d.]+)\s*\n\s*WP1", surfaces),
            "lpd_calc_w_m2": number(r"照明功率密度\s*\n\s*([\d.]+) W/m²", results_page),
            "rug_max": number(r"RUG, max\s*\n\s*([\d.]+)", results_page),
            "rug_target": number(r"RUG, max\s*\n\s*[\d.]+\s*\n\s*≤\s*([\d.]+)", results_page),
            "application": search(r"应用场所:\s*([^\n]+)", results_page),
        }
        luminaire_pages: list[str] = []
        luminaires = _luminaire_table(document[2], luminaire_pages) if len(document) > 2 else []
        totals_page = texts[13] if len(texts) > 13 else ""
        totals = {
            "flux_lm": number(r"Φ总数\s*\n\s*([\d.]+)\s*lm", totals_page),
            "watts": number(r"P总数\s*\n\s*([\d.]+)\s*W", totals_page),
            "efficacy_lm_w": number(r"发光效率\s*\n\s*([\d.]+)\s*lm/W", totals_page),
        }
        positions: dict[str, list[dict[str, Any]]] = {"panels": [], "downlights": []}
        position_pages: list[str] = []
        for page_index in range(3, min(11, len(document))):
            page_text = texts[page_index]
            for match in re.finditer(
                r"1\.光（X / Y / Z）\s*\n\s*([\d.]+) m / ([\d.]+) m /\s*\n\s*([\d.]+) m",
                page_text,
            ):
                positions["panels"].append(
                    {"x_m": float(match.group(1)), "y_m": float(match.group(2)),
                     "z_m": float(match.group(3)), "index": None}
                )
            for match in re.finditer(
                r"([\d.]+) m\s*\n\s*([\d.]+) m\s*\n\s*3\.405 m\s*\n\s*(\d+)",
                page_text,
            ):
                positions["panels"].append(
                    {"x_m": float(match.group(1)), "y_m": float(match.group(2)),
                     "z_m": 3.405, "index": int(match.group(3))}
                )
            position_pages.append(f"page {page_index + 1}")
        unique: dict[tuple[float, float], dict[str, Any]] = {}
        for item in positions["panels"]:
            unique.setdefault((round(item["x_m"], 3), round(item["y_m"], 3)), item)
        positions["panels"] = list(unique.values())
        for page_index in range(11, min(13, len(document))):
            for match in re.finditer(
                r"([\d.]+) m\s*\n\s*([\d.]+) m\s*\n\s*4\.173 m\s*\n\s*(\d+)",
                texts[page_index],
            ):
                positions["downlights"].append(
                    {"x_m": float(match.group(1)), "y_m": float(match.group(2)),
                     "z_m": 4.173, "index": int(match.group(3))}
                )
        warnings: list[str] = []
        for key, value in {
            "workplane_height_m": summary["workplane_height_m"],
            "mounting_height_range_m": summary["mounting_height_range_m"],
            "mean_lx": results["mean_lx"],
            "target_lx": results["target_lx"],
        }.items():
            if not value:
                warnings.append(f"未提取到字段: {key}")
        for index, key in enumerate(("panels", "downlights")):
            expected = luminaires[index].get("count") if index < len(luminaires) else None
            if expected is not None and len(positions[key]) != expected:
                warnings.append(f"{key} 位置数 {len(positions[key])} 与灯具表件数 {expected} 不一致")
        return {
            "source": {
                "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "pages": document.page_count, "creator": document.metadata.get("creator"),
            },
            "summary": summary,
            "results": results,
            "luminaires": luminaires,
            "totals": totals,
            "positions": positions,
            "provenance": {
                "summary": "page 2", "results": "page 3 & 16", "luminaires": luminaire_pages,
                "positions": [position_pages[0], position_pages[-1]] if position_pages else [],
            },
            "warnings": warnings,
        }
    finally:
        document.close()


def translation_stats(left: list[list[float]], right: list[dict[str, Any]]) -> dict[str, float] | None:
    first = sorted(left, key=lambda point: (round(point[1], 1), round(point[0], 1)))
    second = sorted(
        [(float(item["x_m"]), float(item["y_m"])) for item in right],
        key=lambda point: (round(point[1], 1), round(point[0], 1)),
    )
    if not first or len(first) != len(second):
        return None
    dx = [b[0] - a[0] for a, b in zip(first, second)]
    dy = [b[1] - a[1] for a, b in zip(first, second)]
    mean_x, mean_y = sum(dx) / len(dx), sum(dy) / len(dy)
    std_x = (sum((value - mean_x) ** 2 for value in dx) / len(dx)) ** 0.5
    std_y = (sum((value - mean_y) ** 2 for value in dy) / len(dy)) ** 0.5
    return {"delta_x_m": mean_x, "delta_y_m": mean_y, "sigma_x_m": std_x, "sigma_y_m": std_y}


def cross_validate(dxf: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    warnings = [*dxf.get("warnings", []), *report.get("warnings", [])]
    for dxf_key, report_key in (("panel", "panels"), ("downlight", "downlights")):
        stats = translation_stats(dxf["luminaires"].get(dxf_key, []), report["positions"].get(report_key, []))
        checks[dxf_key] = stats
        if stats is None:
            warnings.append(f"{dxf_key} 数量不一致，无法执行 DXF/PDF 平移匹配。")
        elif max(stats["sigma_x_m"], stats["sigma_y_m"]) >= 0.01:
            warnings.append(f"{dxf_key} 的 DXF/PDF 点位不是稳定平移关系。")
    return {"checks": checks, "warnings": list(dict.fromkeys(warnings))[:100]}


__all__ = [
    "DialuxReportParseError",
    "cross_validate",
    "parse_dialux_report",
    "translation_stats",
]
