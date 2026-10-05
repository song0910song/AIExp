"""Safe, local document extraction for the project knowledge base."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from .config import PROJECT_ROOT, Settings
from .schemas import EvidenceBlock, EvidencePage, EvidenceTable


class DocumentLoadError(ValueError):
    pass


_PADDLEOCR_MAX_BATCH_PAGES = 1000


def _paddleocr_error_message(operation: str, error: Exception) -> str:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return (
            f"PaddleOCR authentication failed (HTTP {status_code}). "
            "Check that PADDLEOCR_ACCESS_TOKEN is a valid, current AI Studio access token."
        )
    if status_code is not None:
        details = " ".join(str(getattr(response, "text", "") or "").split())
        suffix = f": {details[:800]}" if details else ""
        return f"PaddleOCR {operation} failed (HTTP {status_code}){suffix}"
    return f"PaddleOCR {operation} failed: {error}"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_path: Path
    source_name: str
    content: str
    sha256: str
    page_count: int | None = None
    pages: tuple[EvidencePage, ...] = ()


class PaddleOCRClient:
    """Client for PaddleOCR's synchronous API and asynchronous v2 jobs endpoint.

    Deployments can supply a compatible endpoint through `PADDLEOCR_API_URL`.
    Text PDFs never make a remote call; scanned pages are submitted as one batch.
    """

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None) -> None:
        self.settings = settings or Settings()
        self.session = session or requests.Session()

    def _auth_headers(self) -> dict[str, str]:
        token = (self.settings.paddleocr_access_token or "").strip()
        if not token:
            raise DocumentLoadError("PADDLEOCR_ACCESS_TOKEN is required for OCR")
        return {"Authorization": f"Bearer {token}"}

    def extract_pdf(self, path: Path) -> str:
        return _extract_text(self.extract_pdf_result(path)) or ""

    def extract_pdf_result(self, path: Path) -> object:
        if not urlsplit(self.settings.paddleocr_api_url).path.rstrip("/").endswith("/jobs"):
            return self._extract_pdf_sync(path)
        return self._extract_pdf_job(path)

    def _extract_pdf_sync(self, path: Path) -> object:
        token = (self.settings.paddleocr_access_token or "").strip()
        if not token:
            raise DocumentLoadError("PADDLEOCR_ACCESS_TOKEN is required for OCR")
        headers = {
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        }
        try:
            payload = {
                "file": base64.b64encode(path.read_bytes()).decode("ascii"),
                "fileType": 0,
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            }
            response = self.session.post(
                self.settings.paddleocr_api_url,
                json=payload,
                headers=headers,
                timeout=self.settings.paddleocr_timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
        except (OSError, requests.RequestException, ValueError) as error:
            raise DocumentLoadError(_paddleocr_error_message("request", error)) from error
        provider_error = _provider_error(result)
        if provider_error:
            raise DocumentLoadError(f"PaddleOCR request was rejected: {provider_error}")
        if not _extract_text(result):
            fields = ", ".join(_response_field_names(result)) or "no fields"
            raise DocumentLoadError(
                f"PaddleOCR response contained no OCR text (response fields: {fields})"
            )
        return result

    def _extract_pdf_job(self, path: Path) -> object:
        try:
            with path.open("rb") as file_handle:
                response = self.session.post(
                    self.settings.paddleocr_api_url,
                    files={"file": (path.name, file_handle, "application/pdf")},
                    data={
                        "model": self.settings.paddleocr_model,
                        "optionalPayload": json.dumps({
                            "useDocOrientationClassify": False,
                            "useDocUnwarping": False,
                            "useChartRecognition": False,
                        }),
                    },
                    headers=self._auth_headers(),
                    timeout=self.settings.paddleocr_timeout_seconds,
                )
            response.raise_for_status()
            payload = response.json()
        except (OSError, requests.RequestException, ValueError) as error:
            raise DocumentLoadError(_paddleocr_error_message("submission", error)) from error
        provider_error = _provider_error(payload)
        if provider_error:
            raise DocumentLoadError(f"PaddleOCR submission was rejected: {provider_error}")
        job_id = (
            _nested_value(payload, "job_id")
            or _nested_value(payload, "task_id")
            or _nested_value(payload, "id")
        )
        if str(_nested_value(payload, "status") or _nested_value(payload, "task_status") or "").casefold() in {
            "failed", "error", "cancelled"
        }:
            raise DocumentLoadError("PaddleOCR submission returned a failed status")
        text = _extract_text(payload)
        if text:
            return payload
        if not job_id:
            fields = ", ".join(_response_field_names(payload)) or "no fields"
            raise DocumentLoadError(
                "PaddleOCR response contained neither OCR text nor a job/task identifier "
                f"(response fields: {fields}); verify PADDLEOCR_API_URL and its response format"
            )
        return self._poll(str(job_id))

    def _poll(self, job_id: str) -> object:
        deadline = time.monotonic() + self.settings.paddleocr_timeout_seconds
        job_url = f"{self.settings.paddleocr_api_url.rstrip('/')}/{quote(job_id, safe='')}"
        while time.monotonic() < deadline:
            try:
                response = self.session.get(
                    job_url,
                    headers=self._auth_headers(),
                    timeout=min(30, self.settings.paddleocr_timeout_seconds),
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                raise DocumentLoadError(_paddleocr_error_message("status request", error)) from error
            provider_error = _provider_error(payload)
            if provider_error:
                raise DocumentLoadError(f"PaddleOCR status request failed: {provider_error}")
            status = str(
                _nested_value(payload, "state")
                or _nested_value(payload, "status")
                or _nested_value(payload, "task_status")
                or ""
            ).casefold()
            if status in {"failed", "error", "cancelled"}:
                reason = _nested_value(payload, "error_msg") or _nested_value(payload, "message")
                detail = f": {reason}" if reason else ""
                raise DocumentLoadError(f"PaddleOCR job {job_id} ended with status {status}{detail}")
            if status in {"done", "completed", "succeeded", "success"}:
                result_url = _nested_value(payload, "json_url")
                if isinstance(result_url, str) and result_url.strip():
                    return self._download_jsonl(result_url.strip())
            text = _extract_text(payload)
            if text and status not in {"pending", "running"}:
                return payload
            if status in {"done", "completed", "succeeded", "success"}:
                raise DocumentLoadError(
                    f"PaddleOCR job {job_id} completed without OCR text or a JSON result URL"
                )
            time.sleep(self.settings.paddleocr_poll_interval_seconds)
        raise DocumentLoadError(f"PaddleOCR job {job_id} timed out")

    def _download_jsonl(self, result_url: str) -> object:
        try:
            response = self.session.get(
                result_url,
                timeout=min(30, self.settings.paddleocr_timeout_seconds),
            )
            response.raise_for_status()
            lines = response.text.splitlines()
            results = []
            for line_number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError as error:
                    raise DocumentLoadError(
                        f"PaddleOCR result JSONL contains invalid JSON on line {line_number}"
                    ) from error
                results.append(item.get("result", item) if isinstance(item, dict) else item)
        except (requests.RequestException, ValueError) as error:
            if isinstance(error, DocumentLoadError):
                raise
            raise DocumentLoadError(_paddleocr_error_message("result download", error)) from error
        if not results:
            raise DocumentLoadError("PaddleOCR completed but returned an empty JSONL result")
        return {"data": {"result": results}}


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
        ocr_targets = []
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
                ocr_targets.append((index, record, prefix))
            elif not text:
                record.status = "empty"
                record.warnings.append("本页无可提取内容，请核对是否为空白页")
            pages.append(record)

        for offset in range(0, len(ocr_targets), _PADDLEOCR_MAX_BATCH_PAGES):
            batch = ocr_targets[offset:offset + _PADDLEOCR_MAX_BATCH_PAGES]
            if hasattr(ocr_client, "extract_pdf_result"):
                _process_ocr_result_batch(path, pdf, ocr_client, batch)
            else:
                for index, record, prefix in batch:
                    try:
                        with tempfile.TemporaryDirectory(prefix="lighting-ocr-page-") as directory:
                            single_path = Path(directory) / f"{prefix}.pdf"
                            _write_pdf_page_batch(pdf, [index], single_path)
                            recognized = ocr_client.extract_pdf(single_path)
                        if not recognized.strip():
                            raise DocumentLoadError("OCR returned an empty result")
                        _attach_ocr_result(record, {"text": recognized}, prefix, recognized)
                    except (DocumentLoadError, OSError, requests.RequestException) as error:
                        _mark_ocr_failure(record, error)
        return pages
    finally:
        pdf.close()


def _process_ocr_result_batch(source_path: Path, pdf, ocr_client, batch) -> None:
    try:
        indexes = [target[0] for target in batch]
        if len(indexes) == len(pdf) and indexes == list(range(len(pdf))):
            payload = ocr_client.extract_pdf_result(source_path)
        else:
            with tempfile.TemporaryDirectory(prefix="lighting-ocr-batch-") as directory:
                batch_path = Path(directory) / "pages.pdf"
                _write_pdf_page_batch(pdf, indexes, batch_path)
                payload = ocr_client.extract_pdf_result(batch_path)
        page_payloads = _ocr_page_results(payload)
        if len(page_payloads) != len(batch):
            raise DocumentLoadError(
                f"OCR returned {len(page_payloads)} page results for {len(batch)} submitted pages"
            )
        recognized_pages = [_extract_text(result) or "" for result in page_payloads]
        if any(not text.strip() for text in recognized_pages):
            raise DocumentLoadError("OCR returned an empty result for one or more pages")
        for (_, record, prefix), result, recognized in zip(batch, page_payloads, recognized_pages):
            _attach_ocr_result(record, result, prefix, recognized)
    except (DocumentLoadError, OSError, requests.RequestException) as error:
        cause = error.__cause__ or error
        response = getattr(cause, "response", None)
        status_code = getattr(response, "status_code", None)
        should_split = (
            len(batch) > 1
            and (status_code in {413, 504}
                 or isinstance(cause, requests.Timeout)
                 or "timed out" in str(error).casefold())
        )
        if should_split:
            midpoint = len(batch) // 2
            _process_ocr_result_batch(source_path, pdf, ocr_client, batch[:midpoint])
            _process_ocr_result_batch(source_path, pdf, ocr_client, batch[midpoint:])
            return
        for _, record, _ in batch:
            _mark_ocr_failure(record, error)


def _write_pdf_page_batch(pdf, page_indexes: list[int], output_path: Path) -> None:
    import fitz

    with fitz.open() as batch_pdf:
        for index in page_indexes:
            batch_pdf.insert_pdf(pdf, from_page=index, to_page=index)
        batch_pdf.save(output_path)


def _ocr_page_results(payload: object) -> list[object]:
    page_results: list[object] = []

    def collect_layout_results(value: object) -> None:
        if isinstance(value, dict):
            layouts = next(
                (child for key, child in value.items()
                 if _normalized_field_name(str(key)) == "layoutparsingresults"),
                None,
            )
            if isinstance(layouts, list):
                page_results.extend(layouts)
                return
            for child in value.values():
                collect_layout_results(child)
        elif isinstance(value, list):
            for child in value:
                collect_layout_results(child)

    collect_layout_results(payload)
    if page_results:
        return page_results
    result_list = _nested_value(payload, "result")
    if isinstance(result_list, list) and result_list and all(_extract_text(item) for item in result_list):
        return result_list
    return [payload] if _extract_text(payload) else []


def _attach_ocr_result(record: EvidencePage, payload: object, prefix: str, recognized: str) -> None:
    record.blocks.append(EvidenceBlock(
        block_id=f"{prefix}-ocr",
        text=recognized,
        kind="ocr",
        coordinate_space="unknown",
    ))
    record.ocr_layout = _ocr_layout(payload, prefix)
    record.tables.extend(_markdown_tables(recognized, f"{prefix}-ocr"))
    record.text = record.text + ("\n\n" if record.text else "") + recognized
    record.status = "ocr_review"
    record.warnings.append("本页由外部 OCR 识别；文字、脚注、表格及布局需对照原页审核")
    if not record.ocr_layout or any(block.bbox is None for block in record.ocr_layout):
        record.warnings.append("OCR 未返回完整 PDF 坐标；布局坐标保留服务坐标系，未推断页面位置")


def _mark_ocr_failure(record: EvidencePage, error: Exception) -> None:
    record.status = "needs_review"
    record.warnings.append(f"本页 OCR 未完成，不能视为读取完整：{error}")


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
        normalized_key = _normalized_field_name(key)
        for candidate, value in payload.items():
            if _normalized_field_name(str(candidate)) == normalized_key:
                return value
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


def _normalized_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _provider_error(payload: object) -> str | None:
    error_code = _nested_value(payload, "error_code")
    if error_code is None:
        error_code = _nested_value(payload, "code")
    if error_code not in (None, 0, "0", 200, "200", "success", "ok", "", False):
        message = (
            _nested_value(payload, "error_msg")
            or _nested_value(payload, "error_message")
            or _nested_value(payload, "message")
            or _nested_value(payload, "msg")
        )
        return str(message or f"provider error code {error_code}")
    success = _nested_value(payload, "success")
    if success is False or (isinstance(success, str) and success.casefold() == "false"):
        message = _nested_value(payload, "error_msg") or _nested_value(payload, "message")
        return str(message or "provider reported success=false")
    return None


def _response_field_names(payload: object, *, limit: int = 12) -> list[str]:
    names: list[str] = []

    def collect(value: object) -> None:
        if len(names) >= limit:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key) not in names:
                    names.append(str(key))
                collect(child)
                if len(names) >= limit:
                    return
        elif isinstance(value, list):
            for child in value[:3]:
                collect(child)
                if len(names) >= limit:
                    return

    collect(payload)
    return names


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
