from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from lighting_agent.deliverables import build_dialux_task_archive
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


def test_task_archive_embeds_persisted_assets_using_luminaire_article_name(tmp_path) -> None:
    state = ProjectState(
        brief=DesignBrief(project_name="Task archive"),
        luminaires=[
            LuminaireCandidate(
                luminaire_id="fixture1",
                article_name="Panel/4000: 16W",
                detail_url="https://luminaires.dialux.com/zh/article/fixture1",
                has_photometry_download=True,
            )
        ],
        selected_luminaire_ids=["fixture1"],
    )

    assets = PhotometryAssetStore(tmp_path / "projects", Downloader(_vendor_zip()))
    asset = assets.download(state, "fixture1")
    assert asset.status == "downloaded"
    assert len(asset.extracted_files) == 2

    archive_bytes = build_dialux_task_archive(state, assets)

    with ZipFile(BytesIO(archive_bytes)) as archive:
        assert set(archive.namelist()) == {
            "dialux-task.json",
            "manifest.json",
            "README.txt",
            "photometry/Panel_4000_ 16W-fixture1.zip",
            "photometry/extracted/Panel_4000_ 16W-fixture1/fixture.uld",
            "photometry/extracted/Panel_4000_ 16W-fixture1/fixture.ies",
        }
        manifest = json.loads(archive.read("dialux-task.json"))
        assert manifest["candidates"][0]["photometry"]["bundle_file"] == "photometry/Panel_4000_ 16W-fixture1.zip"
        assert archive.read("photometry/Panel_4000_ 16W-fixture1.zip") == _vendor_zip()


def test_task_archive_downloads_missing_photometry_before_export(tmp_path) -> None:
    state = ProjectState(
        brief=DesignBrief(project_name="Automatic photometry export"),
        luminaires=[
            LuminaireCandidate(
                luminaire_id="fixture1",
                article_name="Automatic fixture",
                detail_url="https://luminaires.dialux.com/zh/article/fixture1",
                has_photometry_download=True,
            )
        ],
        selected_luminaire_ids=["fixture1"],
    )
    downloader = Downloader(_vendor_zip())
    assets = PhotometryAssetStore(tmp_path / "projects", downloader)

    archive_bytes = build_dialux_task_archive(state, assets)

    assert downloader.calls == ["https://luminaires.dialux.com/zh/article/fixture1"]
    with ZipFile(BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        assert "photometry/Automatic fixture-fixture1.zip" in names
        assert "photometry/extracted/Automatic fixture-fixture1/fixture.ies" in names
        assert "photometry/extracted/Automatic fixture-fixture1/fixture.uld" in names
        manifest = json.loads(archive.read("dialux-task.json"))
        assert manifest["photometry_downloads"]["unavailable"] == []
        assert manifest["candidates"][0]["photometry"]["bundle_status"] == "downloaded"


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


def test_task_archive_downloads_only_final_selected_luminaires(tmp_path) -> None:
    selected = LuminaireCandidate(
        luminaire_id="final-fixture",
        article_name="Final fixture",
        detail_url="https://luminaires.dialux.com/zh/article/final-fixture",
        has_photometry_download=True,
    )
    candidate = LuminaireCandidate(
        luminaire_id="search-candidate",
        article_name="Search candidate",
        detail_url="https://luminaires.dialux.com/zh/article/search-candidate",
        has_photometry_download=True,
    )
    state = ProjectState(
        brief=DesignBrief(project_name="Final selection export"),
        luminaires=[candidate, selected],
        selected_luminaire_ids=[selected.luminaire_id],
    )
    downloader = Downloader(_vendor_zip())
    assets = PhotometryAssetStore(tmp_path / "projects", downloader)

    archive_bytes = build_dialux_task_archive(state, assets)

    assert downloader.calls == [selected.detail_url]
    with ZipFile(BytesIO(archive_bytes)) as archive:
        manifest = json.loads(archive.read("dialux-task.json"))
        assert manifest["selected_luminaire_ids"] == [selected.luminaire_id]
        assert [item["luminaire_id"] for item in manifest["candidates"]] == [selected.luminaire_id]
        assert all("search-candidate" not in name for name in archive.namelist())


def test_task_archive_skips_photometry_until_a_final_luminaire_is_selected(tmp_path) -> None:
    candidate = LuminaireCandidate(
        luminaire_id="search-candidate",
        article_name="Search candidate",
        detail_url="https://luminaires.dialux.com/zh/article/search-candidate",
        has_photometry_download=True,
    )
    state = ProjectState(brief=DesignBrief(project_name="Selection required"), luminaires=[candidate])
    downloader = Downloader(_vendor_zip())

    archive_bytes = build_dialux_task_archive(state, PhotometryAssetStore(tmp_path / "projects", downloader))

    assert downloader.calls == []
    with ZipFile(BytesIO(archive_bytes)) as archive:
        manifest = json.loads(archive.read("dialux-task.json"))
        assert manifest["selection"]["status"] == "pending"
        assert manifest["candidates"] == []


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
