"""Defensive, auditable client for DIALux Luminaire Finder."""

from __future__ import annotations

import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from threading import Lock
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings
from .schemas import (
    DesignBrief,
    LuminaireCandidate,
    LuminaireCriterionCheck,
    LuminaireSearchRequest,
    LuminaireSearchRun,
)


class DialuxAPIError(RuntimeError):
    """A structured external-service failure safe to expose through the API."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "vendor_error",
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


@dataclass(frozen=True, slots=True)
class DialuxSearchResult:
    candidates: list[LuminaireCandidate]
    search_run: LuminaireSearchRun


POWER_PATTERN = re.compile(
    r"(?:(?:system\s*)?power|systemleistung|nennleistung|anschlussleistung|"
    r"(?:系统|额定|额定光源|输入|灯具|总)?功率)\s*[:：]?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:w(?:att(?:s)?)?|瓦(?:特)?)",
    re.I,
)
POWER_VALUE_PATTERN = re.compile(r"(?<![\w/])(\d+(?:[.,]\d+)?)\s*(?:w(?:att(?:s)?)?|瓦(?:特)?)(?!\w)", re.I)
LUMINOUS_FLUX_PATTERN = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*lm(?!\s*/)", re.I)
IP_PATTERN = re.compile(r"\b(IP\s?\d{2}[A-Z]?)\b", re.I)
CCT_PATTERN = re.compile(r"\b(\d{4,5})\s*K\b", re.I)
CRI_PATTERN = re.compile(r"\b(?:CRI|Ra)\s*[:>=]?\s*(\d{2,3})\b", re.I)
UGR_PATTERN = re.compile(r"\bUGR\s*(?:[:<≤]|max(?:\.|imum)?\s*)?\s*(\d{1,2}(?:[.,]\d)?)(?!\d)", re.I)
PHOTOMETRY_ZIP_HREF_PATTERN = re.compile(r"href\s*=\s*['\"](?P<href>[^'\"]+\.zip(?:\?[^'\"]*)?)['\"]", re.I)
MAX_PHOTOMETRY_ZIP_BYTES = 50 * 1024 * 1024
PARSER_VERSION = "2.0"

POWER_FIELD_NAMES = (
    "nominal lamp power",
    "nominal power",
    "system power",
    "power consumption",
    "rated power",
    "input power",
    "connected load",
    "systemleistung",
    "nennleistung",
    "anschlussleistung",
    "leistung",
    "额定光源功率",
    "额定功率",
    "系统功率",
    "输入功率",
    "灯具功率",
    "总功率",
    "功率",
)
LUMINOUS_FLUX_FIELD_NAMES = ("luminous flux", "lamp flux", "total flux")
CHINESE_KEYWORD_FALLBACKS = (
    ("天花板嵌入式", "recessed"),
    ("嵌入式", "recessed"),
    ("筒灯", "downlight"),
    ("射灯", "spotlight"),
    ("轨道灯", "track light"),
    ("面板灯", "panel light"),
    ("线性灯", "linear light"),
    ("灯带", "strip light"),
    ("吊灯", "pendant light"),
    ("吸顶灯", "ceiling light"),
    ("壁灯", "wall light"),
    ("投光灯", "floodlight"),
    ("洗墙灯", "wallwasher"),
)
BRAND_ALIASES = {"philips": "signify", "philipslighting": "signify"}


class DialuxAPI:
    """Bounded, cached DIALux catalogue client.

    The external directory is unversioned HTML/JSON. Every response is parsed
    as untrusted supplier data and converted into a limited, traceable model.
    """

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None) -> None:
        self.settings = settings or Settings()
        self.session = session or self._retrying_session()
        self.base_url = self.settings.dialux_base_url.rstrip("/")
        self.headers = {
            "Accept": "application/json",
            "json": "true",
            "User-Agent": "lighting-design-agent/0.2",
        }
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._cache_lock = Lock()
        self._request_lock = Lock()
        self._next_request_at = 0.0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    @staticmethod
    def _retrying_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _cached(self, key: tuple[Any, ...]) -> tuple[bool, Any | None]:
        with self._cache_lock:
            value = self._cache.get(key)
            if value is None:
                return False, None
            expires_at, payload = value
            if expires_at <= time.monotonic():
                self._cache.pop(key, None)
                return False, None
            return True, payload

    def _cache_result(self, key: tuple[Any, ...], payload: Any) -> None:
        ttl = max(0.0, self.settings.dialux_cache_ttl_seconds)
        if ttl == 0:
            return
        with self._cache_lock:
            if len(self._cache) >= 512:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic() + ttl, payload)

    def _request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        params: dict[str, str] | None = None,
        stream: bool = False,
    ) -> Any:
        with self._request_lock:
            now = time.monotonic()
            if now < self._circuit_open_until:
                raise DialuxAPIError(
                    "DIALux requests are temporarily paused after repeated upstream failures",
                    code="circuit_open",
                    retryable=True,
                )
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + max(
                0.0, self.settings.dialux_min_request_interval_seconds
            )
        if delay:
            time.sleep(delay)
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                stream=stream,
                allow_redirects=False,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            self._record_failure()
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            raise DialuxAPIError(
                f"DIALux request failed: {error}",
                code="upstream_request_failed",
                retryable=status_code is None or status_code == 429 or status_code >= 500,
                status_code=status_code,
            ) from error
        self._record_success()
        return response

    def _record_success(self) -> None:
        with self._request_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        with self._request_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= max(1, self.settings.dialux_circuit_failure_threshold):
                self._circuit_open_until = time.monotonic() + max(
                    1.0, self.settings.dialux_circuit_cooldown_seconds
                )

    def _timeout_for(self, deadline: float | None) -> float:
        if deadline is None:
            return self.settings.dialux_timeout_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DialuxAPIError(
                "DIALux search exceeded its total request deadline",
                code="deadline_exceeded",
                retryable=True,
            )
        return min(self.settings.dialux_timeout_seconds, max(0.1, remaining))

    def list_products(
        self,
        *,
        language: str,
        keyword: str,
        skip: int = 0,
        count: int = 50,
        brand_id: str | None = None,
    ) -> dict[str, Any]:
        payload, _ = self._list_products_with_meta(
            language=language,
            keyword=keyword,
            skip=skip,
            count=count,
            brand_id=brand_id,
            deadline=None,
        )
        return payload

    def _list_products_with_meta(
        self,
        *,
        language: str,
        keyword: str,
        skip: int,
        count: int,
        brand_id: str | None,
        deadline: float | None,
    ) -> tuple[dict[str, Any], bool]:
        if not keyword.strip():
            raise ValueError("A non-empty keyword is required: random DIALux results are not design evidence.")
        count = max(1, min(count, 50))
        key = ("products", language, keyword, skip, count, brand_id)
        hit, cached = self._cached(key)
        if hit:
            return cached, True
        url = f"{self.base_url}/{quote(language, safe='-')}/{skip}/{count}/search/query/a231"
        params = {"ft": keyword}
        if brand_id:
            params["bf"] = brand_id
        response = self._request(
            url,
            params=params,
            headers=self.headers,
            timeout=self._timeout_for(deadline),
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise DialuxAPIError(
                "DIALux product search returned invalid JSON",
                code="invalid_response",
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise DialuxAPIError(
                "DIALux product search returned an unexpected response shape",
                code="response_schema_changed",
            )
        self._cache_result(key, payload)
        return payload, False

    def get_detail(self, luminaire_id: str, *, language: str = "zh") -> dict[str, Any]:
        detail, _, _ = self._detail_with_meta(luminaire_id, language=language, deadline=None)
        return detail

    def _detail_with_meta(
        self, luminaire_id: str, *, language: str, deadline: float | None
    ) -> tuple[dict[str, Any], bool, list[str]]:
        key = ("detail", language, luminaire_id)
        hit, cached = self._cached(key)
        if hit:
            return cached, True, list(cached.get("warnings", []))
        url = f"{self.base_url}/{quote(language, safe='-')}/article/{quote(luminaire_id, safe='_-.')}"
        response = self._request(
            url,
            headers={"Accept": "text/html", "User-Agent": self.headers["User-Agent"]},
            timeout=self._timeout_for(deadline),
        )
        parsed = self._parse_detail_html(response.text)
        self._cache_result(key, parsed)
        return parsed, False, list(parsed.get("warnings", []))

    def search_with_run(
        self,
        request: LuminaireSearchRequest,
        *,
        project_id: str | None = None,
        project_revision: int | None = None,
    ) -> DialuxSearchResult:
        request, missing = validate_luminaire_search(request)
        if missing:
            raise DialuxAPIError(
                "DIALux search is missing deterministic prerequisites: " + ", ".join(missing),
                code="missing_search_conditions",
            )
        started_at = datetime.now(UTC)
        started = time.monotonic()
        deadline = started + max(1.0, self.settings.dialux_search_deadline_seconds)
        run_id = uuid4().hex
        warnings: list[str] = []
        cache_hits = 0
        original_keyword = request.keyword
        resolved_keyword = original_keyword
        fallback_keyword: str | None = None
        brand_resolution: dict[str, str] = {}
        endpoint = f"{self.base_url}/{quote(request.language, safe='-')}/0/50/search/query/a231"

        initial, hit = self._list_products_with_meta(
            language=request.language,
            keyword=original_keyword,
            skip=0,
            count=50,
            brand_id=None,
            deadline=deadline,
        )
        cache_hits += int(hit)
        if self._needs_keyword_fallback(initial):
            fallback_keyword = self._english_keyword_fallback(original_keyword)
            if fallback_keyword:
                fallback_payload, hit = self._list_products_with_meta(
                    language=request.language,
                    keyword=fallback_keyword,
                    skip=0,
                    count=50,
                    brand_id=None,
                    deadline=deadline,
                )
                cache_hits += int(hit)
                if not self._needs_keyword_fallback(fallback_payload):
                    resolved_keyword = fallback_keyword
                    initial = fallback_payload
                else:
                    warnings.append("English keyword fallback also returned no deterministic products.")

        brand_id = request.brand_id
        if request.brand:
            resolved_brand_id = self._resolve_brand_id(initial, request.brand)
            brand_resolution[request.brand] = resolved_brand_id or "not_found"
            if resolved_brand_id is None and brand_id is None:
                warnings.append(f"Required brand {request.brand!r} was not found in DIALux search facets.")
                raw_results: list[dict[str, Any]] = []
            else:
                brand_id = brand_id or resolved_brand_id
                payload, hit = self._list_products_with_meta(
                    language=request.language,
                    keyword=resolved_keyword,
                    skip=0,
                    count=50,
                    brand_id=brand_id,
                    deadline=deadline,
                )
                cache_hits += int(hit)
                raw_results = self._product_items(payload)
        else:
            raw_results = self._product_items(initial)

        pool_size = max(request.max_results, min(50, self.settings.dialux_candidate_pool_size))
        raw_results = raw_results[:pool_size]
        details: dict[str, tuple[dict[str, Any], str, list[str], bool]] = {}
        if raw_results:
            workers = min(max(1, self.settings.dialux_detail_max_workers), len(raw_results))
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dialux-detail")
            futures = {
                executor.submit(self._fetch_detail, item, request.language, deadline): str(item["luminaireId"])
                for item in raw_results
            }
            try:
                for future in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                    luminaire_id = futures[future]
                    try:
                        details[luminaire_id] = future.result()
                    except Exception as error:  # Defensive boundary around one vendor item.
                        details[luminaire_id] = ({}, "failed", [f"detail worker error: {error}"], False)
            except TimeoutError:
                warnings.append("Detail retrieval reached the total DIALux search deadline.")
            finally:
                for future, luminaire_id in futures.items():
                    if luminaire_id not in details:
                        future.cancel()
                        details[luminaire_id] = ({}, "failed", ["detail retrieval deadline exceeded"], False)
                executor.shutdown(wait=False, cancel_futures=True)

        candidates: list[LuminaireCandidate] = []
        for item in raw_results:
            luminaire_id = str(item["luminaireId"])
            detail, detail_status, detail_warnings, detail_cache_hit = details.get(
                luminaire_id, ({}, "failed", ["detail was not scheduled"], False)
            )
            cache_hits += int(detail_cache_hit)
            candidate = self._normalise(
                item,
                detail,
                request,
                search_run_id=run_id,
                detail_status=detail_status,
                parse_warnings=detail_warnings,
            )
            if candidate.matching_status != "rejected":
                candidates.append(candidate)

        candidates = sorted(candidates, key=lambda item: self._sort_key(item, request))[: request.max_results]
        completed_at = datetime.now(UTC)
        run = LuminaireSearchRun(
            search_run_id=run_id,
            project_id=project_id,
            project_revision=project_revision,
            request=request,
            original_keyword=original_keyword,
            resolved_keyword=resolved_keyword,
            fallback_keyword=fallback_keyword if resolved_keyword != original_keyword else None,
            endpoint=endpoint,
            parameters={"ft": resolved_keyword, **({"bf": brand_id} if brand_id else {})},
            brand_resolution=brand_resolution,
            candidate_ids=[item.luminaire_id for item in candidates],
            detail_status_by_id={item.luminaire_id: item.detail_status for item in candidates},
            warnings=warnings,
            parser_version=PARSER_VERSION,
            cache_hits=cache_hits,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return DialuxSearchResult(candidates=candidates, search_run=run)

    def search(self, request: LuminaireSearchRequest) -> list[LuminaireCandidate]:
        """Compatibility wrapper returning only candidates."""

        return self.search_with_run(request).candidates

    def _fetch_detail(
        self, item: dict[str, Any], language: str, deadline: float
    ) -> tuple[dict[str, Any], str, list[str], bool]:
        luminaire_id = str(item["luminaireId"])
        try:
            detail, cache_hit, warnings = self._detail_with_meta(
                luminaire_id, language=language, deadline=deadline
            )
        except DialuxAPIError as error:
            return {}, "failed", [f"{error.code}: {error}"], False
        status = "parse_failed" if warnings else "fetched"
        return detail, status, warnings, cache_hit

    @staticmethod
    def _product_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in payload.get("result", [])
            if isinstance(item, dict) and item.get("luminaireId")
        ]

    @staticmethod
    def _needs_keyword_fallback(payload: dict[str, Any]) -> bool:
        return bool(payload.get("isRandom")) or not payload.get("result")

    @staticmethod
    def _english_keyword_fallback(keyword: str) -> str | None:
        if not re.search(r"[\u4e00-\u9fff]", keyword):
            return None
        terms = [english for chinese, english in CHINESE_KEYWORD_FALLBACKS if chinese in keyword]
        return " ".join(dict.fromkeys(terms)) or None

    @staticmethod
    def _resolve_brand_id(payload: dict[str, Any], wanted_brand: str) -> str | None:
        wanted = _brand_key(wanted_brand)
        for brand in payload.get("brands", []):
            if isinstance(brand, dict) and _brand_key(str(brand.get("name", ""))) == wanted:
                return str(brand.get("id"))
        return None

    def _normalise(
        self,
        item: dict[str, Any],
        detail: dict[str, Any],
        request: LuminaireSearchRequest,
        *,
        search_run_id: str,
        detail_status: str,
        parse_warnings: list[str],
    ) -> LuminaireCandidate:
        technical_summary = str(item.get("technicalSummaryLine") or "")
        summary = str(item.get("summaryLine") or "")
        detail_fields = _safe_detail_fields(detail.get("fields", {}))
        detail_text = [f"{key}: {value}" for key, value in detail_fields.items()]
        article_name = str(item.get("articleName") or item["luminaireId"])
        full_text = " ".join([article_name, summary, technical_summary, *detail_text])
        candidate = LuminaireCandidate(
            luminaire_id=str(item["luminaireId"]),
            article_name=article_name,
            brand_name=_optional_text(item.get("brandName")),
            summary=_optional_text(summary),
            technical_summary=_optional_text(technical_summary),
            power_w=_field_float(detail_fields, POWER_FIELD_NAMES)
            or _first_float(POWER_PATTERN, full_text)
            or _first_float(POWER_VALUE_PATTERN, full_text),
            luminous_flux_lm=_field_float(detail_fields, LUMINOUS_FLUX_FIELD_NAMES)
            or _first_float(LUMINOUS_FLUX_PATTERN, full_text),
            ip_rating=_first_text(IP_PATTERN, full_text),
            cct_k=_field_int(detail_fields, ("cct", "colour temperature", "color temperature"))
            or _first_int(CCT_PATTERN, full_text),
            cri=_field_int(detail_fields, ("cri", "colour rendering index", "color rendering index"))
            or _first_int(CRI_PATTERN, full_text),
            ugr=_field_float(detail_fields, ("ugr", "unified glare rating", "统一眩光值", "眩光指数"))
            or _first_float(UGR_PATTERN, full_text),
            detail_url=self._absolute_url(
                str(item.get("toDetails") or f"/{request.language}/article/{item['luminaireId']}")
            ),
            image_url=self._absolute_url_if_present(item.get("mosaicImage")),
            photometry_image_url=self._absolute_url_if_present(item.get("imageTriplet")),
            has_uld=bool(item.get("hasUld")),
            has_photometry_download=bool(item.get("hasPhotometryDownload")),
            detail_fields=detail_fields,
            search_run_id=search_run_id,
            detail_status=detail_status,  # type: ignore[arg-type]
            parse_warnings=parse_warnings[:50],
            retrieved_at=datetime.now(UTC),
        )
        return match_luminaire_candidate(candidate, request)

    @staticmethod
    def _sort_key(
        candidate: LuminaireCandidate, request: LuminaireSearchRequest
    ) -> tuple[int, int, int, int, int, str]:
        status = {"matches": 0, "incomplete": 1, "rejected": 2}[candidate.matching_status]
        if request.max_ugr is None:
            ugr_rank = 0
        elif candidate.ugr is None:
            ugr_rank = 1
        else:
            ugr_rank = 0 if candidate.ugr <= request.max_ugr else 2
        preferred = int(
            bool(request.preferred_brands)
            and not any(_brand_key(candidate.brand_name or "") == _brand_key(item) for item in request.preferred_brands)
        )
        return (
            status,
            len(candidate.missing_requested_fields),
            ugr_rank,
            preferred,
            -int(candidate.has_uld),
            candidate.article_name.casefold(),
        )

    def _absolute_url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

    def _absolute_url_if_present(self, path: Any) -> str | None:
        return self._absolute_url(str(path)) if path else None

    def _trusted_get(self, url: str, *, headers: dict[str, str], stream: bool = False) -> tuple[str, Any]:
        current = url
        for _ in range(4):
            self._assert_dialux_url(current)
            response = self._request(
                current,
                headers=headers,
                timeout=self.settings.dialux_timeout_seconds,
                stream=stream,
            )
            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                location = getattr(response, "headers", {}).get("Location")
                if not location:
                    raise DialuxAPIError("DIALux redirect did not include a Location header", code="invalid_redirect")
                current = urljoin(current, location)
                continue
            return current, response
        raise DialuxAPIError("DIALux redirect chain exceeded the allowed limit", code="redirect_limit")

    def download_photometry_zip(self, detail_url: str) -> tuple[str, bytes]:
        """Download a same-origin photometric ZIP with a hard streamed size limit."""

        page_url, detail_response = self._trusted_get(
            detail_url,
            headers={"Accept": "text/html", "User-Agent": self.headers["User-Agent"]},
        )
        match = PHOTOMETRY_ZIP_HREF_PATTERN.search(detail_response.text)
        if match is None:
            raise DialuxAPIError(
                "DIALux product page does not expose a photometric ZIP download",
                code="photometry_not_listed",
            )
        download_url = urljoin(page_url, match.group("href"))
        source_url, download_response = self._trusted_get(
            download_url,
            headers={"Accept": "application/zip, application/octet-stream"},
            stream=True,
        )
        content = _read_limited_response(download_response, MAX_PHOTOMETRY_ZIP_BYTES)
        if not content:
            raise DialuxAPIError("DIALux photometric ZIP download was empty", code="empty_download")
        if not zipfile.is_zipfile(BytesIO(content)):
            raise DialuxAPIError("DIALux photometric download was not a valid ZIP file", code="invalid_zip")
        return source_url, content

    def _assert_dialux_url(self, value: str) -> None:
        candidate = urlparse(value)
        trusted = urlparse(self.base_url)
        if candidate.scheme != "https" or candidate.netloc != trusted.netloc:
            raise DialuxAPIError(
                "DIALux photometric download URL is outside the configured DIALux host",
                code="untrusted_download_url",
            )

    @staticmethod
    def _parse_detail_html(html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        fields: dict[str, str] = {}
        for table in soup.select("table"):
            headers: list[str] | None = None
            for row in table.select("tr"):
                cells = row.find_all(["th", "td"], recursive=False)
                values = [cell.get_text(" ", strip=True) for cell in cells]
                if len(values) >= 2 and all(cell.name == "th" for cell in cells):
                    headers = values
                    continue
                if headers and len(values) >= len(headers):
                    for header, value in zip(headers, values[-len(headers) :], strict=True):
                        if header and value:
                            fields.setdefault(header, value)
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
            if len(cells) >= 2 and cells[0] and cells[1]:
                fields.setdefault(cells[0], " | ".join(cells[1:]))
        for term in soup.select("dt"):
            definition = term.find_next_sibling("dd")
            if definition:
                key = term.get_text(" ", strip=True)
                value = definition.get_text(" ", strip=True)
                if key and value:
                    fields[key] = value
        warnings = [] if fields else ["No recognised technical field table was found on the detail page."]
        title = soup.title.get_text(" ", strip=True) if soup.title else None
        return {
            "title": title,
            "fields": fields,
            "warnings": warnings,
        }


def _read_limited_response(response: Any, limit: int) -> bytes:
    iterator = getattr(response, "iter_content", None)
    chunks = iterator(chunk_size=64 * 1024) if callable(iterator) else [response.content]
    content = bytearray()
    try:
        for chunk in chunks:
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > limit:
                raise DialuxAPIError(
                    f"DIALux photometric ZIP exceeds the {limit // (1024 * 1024)} MB package limit",
                    code="download_too_large",
                )
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return bytes(content)


def _safe_detail_fields(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    fields: dict[str, str] = {}
    for key, field_value in value.items():
        if len(fields) >= 50:
            break
        name = str(key).strip()[:160]
        text = str(field_value).strip()[:500]
        if name and text:
            fields[name] = text
    return fields


def _first_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    return float(match.group(1).replace(",", ".")) if match else None


def _first_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match.group(1)) if match else None


def _first_text(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).upper().replace(" ", "") if match else None


def _field_value(fields: dict[str, str], names: tuple[str, ...]) -> str | None:
    aliases = {item.casefold() for item in names}
    for key, value in fields.items():
        if key.casefold().strip() in aliases:
            return value
    return None


def _field_float(fields: dict[str, str], names: tuple[str, ...]) -> float | None:
    value = _field_value(fields, names)
    if not value:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", value)
    return float(match.group(0).replace(",", ".")) if match else None


def _field_int(fields: dict[str, str], names: tuple[str, ...]) -> int | None:
    value = _field_value(fields, names)
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value else ""
    return text or None


def _brand_key(value: str) -> str:
    normalized = re.sub(r"[\W_]+", "", value.casefold())
    return BRAND_ALIASES.get(normalized, normalized)


def _ip_components(value: str) -> tuple[int, int] | None:
    match = re.search(r"IP\s?(\d)(\d)", value, re.I)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _ip_meets_minimum(candidate: str, requirement: str) -> bool:
    candidate_parts = _ip_components(candidate)
    requirement_parts = _ip_components(requirement)
    if candidate_parts is None or requirement_parts is None:
        return False
    return candidate_parts[0] >= requirement_parts[0] and candidate_parts[1] >= requirement_parts[1]


def apply_brief_constraints(request: LuminaireSearchRequest, brief: DesignBrief) -> LuminaireSearchRequest:
    """Fill the key lighting conditions (illuminance/CCT/CRI/UGR) from the confirmed brief.

    Power, IP and brand preferences are deliberately not inferred: those
    conditions are applied only when the caller states them explicitly.
    """

    inferred = {
        "target_illuminance_lx": brief.target_illuminance_lx,
        "target_cct_k": brief.target_cct_k,
        "min_cri": brief.min_cri,
        "max_ugr": brief.target_ugr,
    }
    updates = {
        name: value
        for name, value in inferred.items()
        if getattr(request, name) is None and value is not None
    }
    return request.model_copy(update=updates)


def validate_luminaire_search(
    request: LuminaireSearchRequest, brief: DesignBrief | None = None
) -> tuple[LuminaireSearchRequest, list[str]]:
    """Return an effective request and missing deterministic prerequisites.

    The key lighting conditions are illuminance, CCT, CRI and UGR; any of
    them (or an explicitly stated brand/IP/power condition) can unlock a
    catalogue search.
    """

    effective = apply_brief_constraints(request, brief) if brief is not None else request
    missing: list[str] = []
    if brief is not None and not brief.space_type:
        missing.append("space_type")
    if not any(
        (
            effective.target_illuminance_lx is not None,
            effective.target_cct_k is not None,
            effective.min_cri is not None,
            effective.max_ugr is not None,
            effective.min_ip_rating is not None,
            effective.max_power_w is not None,
            effective.brand is not None,
        )
    ):
        missing.append("selection_constraint")
    return effective, missing


def candidate_summary(candidate: LuminaireCandidate) -> dict[str, Any]:
    """Return bounded supplier facts suitable for an LLM tool result."""

    brief_validation = candidate.brief_validation
    return {
        "luminaire_id": candidate.luminaire_id,
        "article_name": candidate.article_name,
        "brand_name": candidate.brand_name,
        "power_w": candidate.power_w,
        "luminous_flux_lm": candidate.luminous_flux_lm,
        "cct_k": candidate.cct_k,
        "cri": candidate.cri,
        "ugr": candidate.ugr,
        "ip_rating": candidate.ip_rating,
        "has_uld": candidate.has_uld,
        "has_photometry_download": candidate.has_photometry_download,
        "matching_status": candidate.matching_status,
        "missing_requested_fields": candidate.missing_requested_fields,
        "failed_requested_fields": candidate.failed_requested_fields,
        "criteria_checks": [item.model_dump(mode="json") for item in candidate.criteria_checks],
        "detail_status": candidate.detail_status,
        "parse_warnings": candidate.parse_warnings,
        "detail_url": candidate.detail_url,
        "project_brief_matching_status": (
            brief_validation.matching_status if brief_validation is not None else None
        ),
        "project_brief_missing_fields": (
            brief_validation.missing_requested_fields if brief_validation is not None else []
        ),
        "project_brief_failed_fields": (
            brief_validation.failed_requested_fields if brief_validation is not None else []
        ),
    }


def match_luminaire_candidate(
    candidate: LuminaireCandidate, request: LuminaireSearchRequest
) -> LuminaireCandidate:
    """Classify requested attributes, keeping missing supplier data distinct from failures."""

    if candidate.luminous_flux_lm is None:
        detail_text = " ".join(f"{key}: {value}" for key, value in candidate.detail_fields.items())
        flux = _field_float(candidate.detail_fields, LUMINOUS_FLUX_FIELD_NAMES) or _first_float(
            LUMINOUS_FLUX_PATTERN,
            " ".join(part for part in (candidate.summary, candidate.technical_summary, detail_text) if part),
        )
        if flux is not None:
            candidate = candidate.model_copy(update={"luminous_flux_lm": flux})

    missing: list[str] = []
    failed: list[str] = []
    checks: list[LuminaireCriterionCheck] = []

    def add_check(
        field: str,
        expected: str,
        observed: str | None,
        passed: bool | None,
        *,
        priority: str = "required",
    ) -> None:
        status = "unknown" if passed is None else "pass" if passed else "fail"
        checks.append(
            LuminaireCriterionCheck(
                field=field,  # type: ignore[arg-type]
                status=status,
                expected=expected,
                observed=observed,
                priority=priority,  # type: ignore[arg-type]
            )
        )
        if priority != "required":
            return
        if passed is None:
            missing.append(field)
        elif not passed:
            failed.append(field)

    if candidate.detail_status in {"failed", "parse_failed"}:
        add_check("detail", "parsed DIALux detail page", candidate.detail_status, None)
    if request.brand is not None:
        add_check(
            "brand",
            request.brand,
            candidate.brand_name,
            _brand_key(candidate.brand_name) == _brand_key(request.brand) if candidate.brand_name else None,
        )
    if request.max_power_w is not None:
        add_check(
            "max_power_w",
            f"<= {request.max_power_w:g} W",
            f"{candidate.power_w:g} W" if candidate.power_w is not None else None,
            candidate.power_w <= request.max_power_w if candidate.power_w is not None else None,
        )
    if request.target_cct_k is not None:
        tolerance = request.target_cct_tolerance_k
        expected = f"{request.target_cct_k} K" if not tolerance else f"{request.target_cct_k} +/- {tolerance} K"
        add_check(
            "target_cct_k",
            expected,
            f"{candidate.cct_k} K" if candidate.cct_k is not None else None,
            abs(candidate.cct_k - request.target_cct_k) <= tolerance if candidate.cct_k is not None else None,
        )
    if request.min_cri is not None:
        add_check(
            "min_cri",
            f">= {request.min_cri}",
            str(candidate.cri) if candidate.cri is not None else None,
            candidate.cri >= request.min_cri if candidate.cri is not None else None,
        )
    if request.max_ugr is not None:
        add_check(
            "max_ugr",
            f"<= {request.max_ugr:g}",
            f"{candidate.ugr:g}" if candidate.ugr is not None else None,
            candidate.ugr <= request.max_ugr if candidate.ugr is not None else None,
            priority="preference",
        )
    if request.min_ip_rating is not None:
        add_check(
            "min_ip_rating",
            f">= {request.min_ip_rating.upper()}",
            candidate.ip_rating,
            _ip_meets_minimum(candidate.ip_rating, request.min_ip_rating) if candidate.ip_rating else None,
        )

    status = "rejected" if failed else "incomplete" if missing else "matches"
    return candidate.model_copy(
        update={
            "matching_status": status,
            "missing_requested_fields": missing,
            "failed_requested_fields": failed,
            "criteria_checks": checks,
        }
    )
