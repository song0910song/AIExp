from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from lighting_agent.dialux_api import DialuxAPI, DialuxAPIError
from lighting_agent.photometry_assets import PhotometryAssetStore
from lighting_agent.schemas import DesignBrief, LuminaireCandidate, ProjectState


class Response:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes = b"",
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None


class DIALuxDownloadSession:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> Response:
        self.calls.append(url)
        if url.endswith("/article/fixture-1"):
            return Response(text='<a href="/files/fixture-1.zip">Download file</a>')
        return Response(content=self.payload)


class RedirectSession:
    def get(self, _url: str, **_kwargs: Any) -> Response:
        return Response(
            status_code=302,
            headers={"Location": "https://example.test/outside.zip"},
        )


class Downloader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.fail = False
        self.calls: list[str] = []

    def download_photometry_zip(self, detail_url: str) -> tuple[str, bytes]:
        self.calls.append(detail_url)
        if self.fail:
            raise RuntimeError("upstream download failed")
        return f"{detail_url}/download.zip", self.payload


def _vendor_zip() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("fixture.uld", "photometry")
        archive.writestr("fixture.ies", "photometry")
        archive.writestr("readme.txt", "metadata")
    return buffer.getvalue()


def _compressed_bomb_zip() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("bomb.ies", "0" * (2 * 1024 * 1024))
    return buffer.getvalue()


def test_dialux_downloads_the_zip_linked_by_the_product_page() -> None:
    payload = _vendor_zip()
    session = DIALuxDownloadSession(payload)

    source_url, content = DialuxAPI(session=session).download_photometry_zip(
        "https://luminaires.dialux.com/zh/article/fixture-1"
    )

    assert source_url == "https://luminaires.dialux.com/files/fixture-1.zip"
    assert content == payload
    assert session.calls == [
        "https://luminaires.dialux.com/zh/article/fixture-1",
        "https://luminaires.dialux.com/files/fixture-1.zip",
    ]


def test_dialux_rejects_cross_host_redirects_before_download() -> None:
    with pytest.raises(DialuxAPIError, match="outside the configured DIALux host"):
        DialuxAPI(session=RedirectSession()).download_photometry_zip(
            "https://luminaires.dialux.com/zh/article/fixture-1"
        )






def test_failed_download_is_persisted_and_can_be_retried(tmp_path) -> None:
    state = ProjectState(
        brief=DesignBrief(project_name="Retry assets"),
        luminaires=[
            LuminaireCandidate(
                luminaire_id="fixture1",
                article_name="Fixture 1",
                detail_url="https://luminaires.dialux.com/zh/article/fixture1",
                has_photometry_download=True,
            )
        ],
        selected_luminaire_ids=["fixture1"],
    )
    downloader = Downloader(_vendor_zip())
    downloader.fail = True
    assets = PhotometryAssetStore(tmp_path / "projects", downloader)

    assert assets.download(state, "fixture1").status == "failed"
    downloader.fail = False
    assert assets.download(state, "fixture1").status == "downloaded"
    assert assets.list_assets(state)[0].status == "downloaded"




def test_design_assets_allow_traced_saved_candidates_without_final_selection(tmp_path) -> None:
    candidate = LuminaireCandidate(
        luminaire_id="design-candidate",
        article_name="Design candidate",
        detail_url="https://luminaires.dialux.com/zh/article/design-candidate",
        has_photometry_download=True,
    )
    state = ProjectState(
        brief=DesignBrief(project_name="Design evaluation assets"),
        luminaires=[candidate],
    )
    downloader = Downloader(_vendor_zip())
    assets = PhotometryAssetStore(tmp_path / "projects", downloader)

    [asset] = assets.ensure_design_assets(state, [candidate.luminaire_id], "design-run-1")

    assert asset.status == "downloaded"
    assert asset.purposes == ["design_evaluation"]
    assert asset.design_run_ids == ["design-run-1"]
    assert downloader.calls == [candidate.detail_url]
    with pytest.raises(ValueError, match="final selected"):
        assets.download(state, candidate.luminaire_id)




def test_photometry_store_rejects_compression_bombs(tmp_path) -> None:
    state = ProjectState(
        brief=DesignBrief(project_name="Zip limits"),
        luminaires=[
            LuminaireCandidate(
                luminaire_id="fixture1",
                article_name="Fixture",
                detail_url="https://luminaires.dialux.com/zh/article/fixture1",
                has_photometry_download=True,
            )
        ],
        selected_luminaire_ids=["fixture1"],
    )

    asset = PhotometryAssetStore(tmp_path / "projects", Downloader(_compressed_bomb_zip())).download(
        state, "fixture1"
    )

    assert asset.status == "failed"
    assert asset.error is not None
    assert "compression ratio" in asset.error
