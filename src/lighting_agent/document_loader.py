"""Safe, local document extraction for the project knowledge base."""

from __future__ import annotations

import hashlib
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import PROJECT_ROOT, Settings
from .schemas import EvidenceBlock, EvidencePage, EvidenceTable


class DocumentLoadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_path: Path
    source_name: str
    content: str
    sha256: str
    page_count: int | None = None
    pages: tuple[EvidencePage, ...] = ()


class PaddleOCRClient:
    """Minimal polling client for a configured PaddleOCR job endpoint.

    Deployments can supply a compatible endpoint through `PADDLEOCR_API_URL`.
    Text PDFs never make a remote call; this client is only a scan fallback.
    """

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None) -> None:
        self.settings = settings or Settings()
        self.session = session or requests.Session()

    def extract_pdf(self, path: Path) -> str:
        return _extract_text(self.extract_pdf_result(path)) or ""

    def extract_pdf_result(self, path: Path) -> object:
        try:
            with path.open("rb") as file_handle:
                response = self.session.post(
                    self.settings.paddleocr_api_url,
                    files={"file": (path.name, file_handle, "application/pdf")},
                    data={"model": self.settings.paddleocr_model},
                    timeout=self.settings.paddleocr_timeout_seconds,
                )
            response.raise_for_status()
            payload = response.json()
        except (OSError, requests.RequestException, ValueError) as error:
            raise DocumentLoadError(f"PaddleOCR submission failed: {error}") from error
        job_id = _nested_value(payload, "job_id") or _nested_value(payload, "id")
        if str(_nested_value(payload, "status")).casefold() in {"failed", "error", "cancelled"}:
            raise DocumentLoadError("PaddleOCR submission returned a failed status")
        text = _extract_text(payload)
        if text:
            return payload
        if not job_id:
            raise DocumentLoadError("PaddleOCR response contained neither text nor a job identifier")
        return self._poll(str(job_id))

    def _poll(self, job_id: str) -> object:
        deadline = time.monotonic() + self.settings.paddleocr_timeout_seconds
        job_url = f"{self.settings.paddleocr_api_url.rstrip('/')}/{job_id}"
        while time.monotonic() < deadline:
            try:
                response = self.session.get(job_url, timeout=min(30, self.settings.paddleocr_timeout_seconds))
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                raise DocumentLoadError(f"PaddleOCR status request failed: {error}") from error
            status = str(_nested_value(payload, "status") or "").casefold()
            if status in {"failed", "error", "cancelled"}:
                raise DocumentLoadError(f"PaddleOCR job {job_id} ended with status {status}")
            text = _extract_text(payload)
            if text:
                return payload
            time.sleep(self.settings.paddleocr_poll_interval_seconds)
        raise DocumentLoadError(f"PaddleOCR job {job_id} timed out")


def _checked_path(file_path: str | Path, allowed_root: Path = PROJECT_ROOT) -> Path:
    path = Path(file_path).expanduser().resolve()
    try:
        path.relative_to(allowed_root.resolve())
    except ValueError as error:
        raise DocumentLoadError(f"Document must be within {allowed_root}") from error
    if not path.is_file():
        raise DocumentLoadError(f"Document does not exist or is not a file: {path}")
    return path


def load_document(
    file_path: str | Path,
    *,
    allowed_root: Path = PROJECT_ROOT,
    ocr_client: PaddleOCRClient | None = None,
) -> ParsedDocument:
    """Read an approved project document without executing embedded content."""

    path = _checked_path(file_path, allowed_root)
    suffix = path.suffix.lower()
    pages: list[EvidencePage] = []
    if suffix in {".md", ".txt"}:
        content, page_count = path.read_text(encoding="utf-8"), None
        lines = content.splitlines()
        blocks = []
        for start in range(0, len(lines), 40):
            text = "\n".join(lines[start:start + 40])
            blocks.append(EvidenceBlock(block_id=f"lines-{start + 1}-{min(start + 40, len(lines))}", text=text))
        if lines:
            pages.append(EvidencePage(locator=f"行 1–{len(lines)}", text=content, blocks=blocks, tables=_markdown_tables(content, "body")))
    elif suffix == ".docx":
        content, page_count = _read_docx(path)
        from docx import Document
        document = Document(path)
        for index, paragraph in enumerate(document.paragraphs, 1):
            if paragraph.text.strip():
                pages.append(EvidencePage(locator=f"段落 {index}", text=paragraph.text,
                    blocks=[EvidenceBlock(block_id=f"paragraph-{index}", text=paragraph.text)]))
        for index, table in enumerate(document.tables, 1):
            cells = [[c.text for c in row.cells] for row in table.rows]
            text = "\n".join(" | ".join(row) for row in cells)
            pages.append(EvidencePage(locator=f"表 {index}", text=text,
                blocks=[EvidenceBlock(block_id=f"table-{index}", text=text, kind="table")],
                tables=[EvidenceTable(table_id=f"table-{index}", cells=cells)]))
    elif suffix == ".pdf":
        pages = _read_pdf_pages(path, ocr_client or PaddleOCRClient())
        content = "\n\n".join(page.text for page in pages if page.text)
        page_count = len(pages)
    else:
        raise DocumentLoadError("Supported document types are .pdf, .docx, .md and .txt")
    content = content.strip()
    if not content and not pages:
        raise DocumentLoadError(
            "No extractable text was found. For a scanned PDF, run it through the configured OCR service first."
        )
    return ParsedDocument(
        source_path=path,
        source_name=path.name,
        content=content,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        page_count=page_count,
        pages=tuple(pages),
    )


def _read_docx(path: Path) -> tuple[str, None]:
    try:
        from docx import Document
    except ImportError as error:  # pragma: no cover - installation issue
        raise DocumentLoadError("python-docx is required to read .docx files") from error
    document = Document(path)
    sections = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                sections.append(" | ".join(cells))
    return "\n\n".join(sections), None


def _read_pdf_pages(path: Path, ocr_client) -> list[EvidencePage]:
    try:
        import fitz  # PyMuPDF
    except ImportError as error:  # pragma: no cover - installation issue
        raise DocumentLoadError("PyMuPDF is required to read .pdf files") from error
    try:
        pdf = fitz.open(path)
    except Exception as error:
        raise DocumentLoadError(f"无法读取 PDF：{error}") from error
    try:
        if pdf.needs_pass:
            raise DocumentLoadError("PDF 已加密，请提供已解密副本")
        pages = []
        for index, page in enumerate(pdf):
            prefix = f"p{index + 1}"
            text = page.get_text("text", sort=True).strip()
            blocks = [EvidenceBlock(block_id=f"{prefix}-b{n}", text=b[4].strip(), bbox=tuple(b[:4]))
                      for n, b in enumerate(page.get_text("blocks", sort=True), 1) if b[6] == 0 and b[4].strip()]
            record = EvidencePage(page_number=index + 1, locator=f"第 {index + 1} 页", text=text,
                                  width=page.rect.width, height=page.rect.height, blocks=blocks)
            try:
                tables = page.find_tables()
                record.tables = [EvidenceTable(table_id=f"{prefix}-t{n}", cells=t.extract(), bbox=tuple(t.bbox),
                    cell_bboxes=[tuple(c) if c else None for c in t.cells]) for n, t in enumerate(tables.tables, 1)]
            except Exception as error:
                record.warnings.append(f"表格结构需人工核对：{error}")
            images = page.get_image_info()
            # Even a small raster region can contain the only applicable table or
            # footnote. Image size is not evidence that it is merely decorative.
            needs_ocr = bool(images) or "\ufffd" in text or (not text and bool(page.get_drawings()))
            if needs_ocr:
                try:
                    with tempfile.TemporaryDirectory(prefix="lighting-ocr-page-") as directory:
                        single_path = Path(directory) / f"{prefix}.pdf"
                        with fitz.open() as single:
                            single.insert_pdf(pdf, from_page=index, to_page=index)
                            single.save(single_path)
                        payload = {}
                        if hasattr(ocr_client, "extract_pdf_result"):
                            payload = ocr_client.extract_pdf_result(single_path)
                            recognized = _extract_text(payload) or ""
                        else:
                            recognized = ocr_client.extract_pdf(single_path)
                    if not recognized.strip():
                        raise DocumentLoadError("OCR 未返回正文")
                    # Preserve native and recognized evidence separately. Do not attach a
                    # fabricated bounding box to an OCR service's plain-text response.
                    record.blocks.append(EvidenceBlock(block_id=f"{prefix}-ocr", text=recognized, kind="ocr", coordinate_space="unknown"))
                    record.ocr_layout = _ocr_layout(payload, prefix)
                    record.tables.extend(_markdown_tables(recognized, f"{prefix}-ocr"))
                    record.text = text + ("\n\n" if text else "") + recognized
                    record.status = "ocr_review"
                    record.warnings.append("本页已逐页外部 OCR；文字、脚注、表格及布局需核对原页")
                    if not record.ocr_layout or any(b.bbox is None for b in record.ocr_layout):
                        record.warnings.append("OCR 未返回完整的 PDF 坐标；原始布局坐标按服务坐标系保留，不伪造页面位置")
                except (DocumentLoadError, OSError, requests.RequestException) as error:
                    record.status = "needs_review"
                    record.warnings.append(f"本页 OCR 未完成，不能视为读取完整：{error}")
            elif not text:
                record.status = "empty"
                record.warnings.append("本页无可提取内容，请核对是否为空白页")
            pages.append(record)
        return pages
    finally:
        pdf.close()


def _markdown_tables(text: str, prefix: str) -> list[EvidenceTable]:
    tables = []
    current = []
    for line in [*text.splitlines(), ""]:
        if "|" in line:
            cells = [v.strip() for v in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            current.append(cells)
        elif current:
            if len(current) > 1:
                tables.append(EvidenceTable(table_id=f"{prefix}-table-{len(tables) + 1}", cells=current))
            current = []
    if "<table" in text.lower():
        from bs4 import BeautifulSoup
        for table in BeautifulSoup(text, "html.parser").find_all("table"):
            grid = {}
            for row_index, row in enumerate(table.find_all("tr")):
                column = 0
                for cell in row.find_all(["th", "td"], recursive=False):
                    while (row_index, column) in grid:
                        column += 1
                    def span(key):
                        try:
                            return min(100, max(1, int(cell.get(key, 1))))
                        except (ValueError, TypeError):
                            return 1
                    rows, columns = span("rowspan"), span("colspan")
                    for r in range(row_index, row_index + rows):
                        for c in range(column, column + columns):
                            grid[r, c] = cell.get_text(" ", strip=True)
                    column += columns
            if grid:
                cells = [[grid.get((r, c), "") for c in range(max(c for _, c in grid) + 1)]
                         for r in range(max(r for r, _ in grid) + 1)]
                tables.append(EvidenceTable(table_id=f"{prefix}-html-{len(tables) + 1}", cells=cells))
    return tables


def _ocr_layout(payload: object, prefix: str) -> list[EvidenceBlock]:
    result = []
    coordinate_space = _nested_value(payload, "coordinate_space")
    if coordinate_space not in {"pdf_points", "image_pixels"}:
        coordinate_space = "unknown"
    def collect(item):
        if isinstance(item, dict):
            box = item.get("block_bbox", item.get("bbox"))
            text = item.get("block_content", item.get("text"))
            if isinstance(text, str) and isinstance(box, (list, tuple)) and len(box) == 4 and all(isinstance(v, (float, int)) and math.isfinite(v) for v in box):
                result.append(EvidenceBlock(block_id=f"{prefix}-layout-{len(result) + 1}", text=text, kind="ocr",
                    bbox=tuple(box) if coordinate_space == "pdf_points" else None, source_bbox=tuple(box), coordinate_space=coordinate_space))
            for value in item.values():
                collect(value)
        elif isinstance(item, list):
            for value in item:
                collect(value)
    collect(payload)
    return result


def _nested_value(payload: object, key: str) -> object | None:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            nested = _nested_value(value, key)
            if nested is not None:
                return nested
    if isinstance(payload, list):
        for value in payload:
            nested = _nested_value(value, key)
            if nested is not None:
                return nested
    return None


def _extract_text(payload: object) -> str | None:
    """Prefer full text, otherwise collect ALL block texts, never only the first."""
    if isinstance(payload, dict):
        for key in ("markdown", "text", "content", "block_content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if key == "markdown" and isinstance(value, dict):
                full_text = _extract_text(value)
                if full_text:
                    return full_text
        if isinstance(payload.get("rec_texts"), list):
            return "\n".join(t for t in payload["rec_texts"] if isinstance(t, str)) or None
        parts = [_extract_text(v) for v in payload.values() if isinstance(v, (dict, list))]
        return "\n\n".join(p for p in parts if p) or None
    if isinstance(payload, list):
        parts = [_extract_text(item) for item in payload]
        return "\n\n".join(p for p in parts if p) or None
    return None
