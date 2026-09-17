"""Validation and text extraction for uploaded DIALux result evidence."""

from __future__ import annotations

import re
from pathlib import Path

from .document_loader import DocumentLoadError, load_document


MAX_DIALUX_RESULT_BYTES = 50 * 1024 * 1024
ALLOWED_DIALUX_RESULT_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp"})

_ILLUMINANCE_PATTERNS = (
    re.compile(
        r"(?:maintained\s+(?:average\s+)?illuminance|average\s+illuminance|维持照度|平均照度)"
        r"[^0-9]{0,40}(\d+(?:[.,]\d+)?)\s*(?:lx|lux|勒克斯)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Ē(?:直角)?[^0-9]{0,120}(\d+(?:[.,]\d+)?)\s*(?:lx|lux|勒克斯)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:E(?:m|av|avg)|Ē)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(?:lx|lux)", re.IGNORECASE),
)


class DialuxResultError(ValueError):
    pass


def validate_dialux_result_bytes(suffix: str, content: bytes) -> str:
    """Check extension and file signature; return a stable media type."""

    normalized = suffix.casefold()
    if normalized not in ALLOWED_DIALUX_RESULT_SUFFIXES:
        raise DialuxResultError("仅支持 DIALux PDF 设计报告或 PNG/JPG/WEBP 仿真图片")
    if not content:
        raise DialuxResultError("DIALux 结果文件不能为空")
    if len(content) > MAX_DIALUX_RESULT_BYTES:
        raise DialuxResultError("DIALux 结果文件不能超过 50 MB")
    signatures = {
        ".pdf": (b"%PDF-", "application/pdf"),
        ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
        ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    }
    if normalized == ".webp":
        if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
            raise DialuxResultError("文件内容与 WEBP 扩展名不匹配")
        return "image/webp"
    signature, media_type = signatures[normalized]
    if not content.startswith(signature):
        raise DialuxResultError(f"文件内容与 {normalized.upper()} 扩展名不匹配")
    return media_type


def extract_maintained_illuminance_from_pdf(path: Path, *, allowed_root: Path) -> float | None:
    """Extract an explicitly labelled maintained/average illuminance value."""

    try:
        document = load_document(path, allowed_root=allowed_root)
    except DocumentLoadError:
        return None
    for pattern in _ILLUMINANCE_PATTERNS:
        match = pattern.search(document.content)
        if match:
            return float(match.group(1).replace(",", "."))
    return None
