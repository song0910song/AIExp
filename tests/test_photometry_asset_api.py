from __future__ import annotations

from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient

from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import LuminaireCandidate
from lighting_agent.web_api import create_app


class FakeDialux:
    def __init__(self) -> None:
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            archive.writestr("fixture.ies", "photometry")
        self.payload = buffer.getvalue()

    def search(self, _request: Any) -> list[LuminaireCandidate]:
        return [
            LuminaireCandidate(
                luminaire_id="fixture1",
                article_name="Fixture 1",
                detail_url="https://luminaires.example.test/fixture1",
                cct_k=4000,
                detail_status="fetched",
                matching_status="matches",
                has_photometry_download=True,
            )
        ]

    def download_photometry_zip(self, detail_url: str) -> tuple[str, bytes]:
        return f"{detail_url}/photometry.zip", self.payload


def test_photometry_asset_endpoints_download_list_file_and_remove(tmp_path) -> None:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=FakeDialux(),
    )
    client = TestClient(app)
    project = client.post(
        "/api/projects",
        json={
            "project_name": "Asset API",
            "space_type": "会议室",
            "mounting": "recessed",
            "target_cct_k": 4000,
        },
    ).json()
    project_id = project["project_id"]
    saved = client.post(
        f"/api/projects/{project_id}/luminaires",
        json={"keyword": "fixture", "expected_revision": 0},
    ).json()["project"]

    rejected = client.post(f"/api/projects/{project_id}/luminaires/fixture1/photometry")
    assert rejected.status_code == 422

    selected = client.put(
        f"/api/projects/{project_id}/selected-luminaires",
        json={"expected_revision": saved["revision"], "luminaire_ids": ["fixture1"]},
    )
    assert selected.status_code == 200

    before = client.get(f"/api/projects/{project_id}/photometry")
    assert before.json()["assets"][0]["status"] == "pending"

    downloaded = client.post(f"/api/projects/{project_id}/luminaires/fixture1/photometry")
    assert downloaded.status_code == 200
    assert downloaded.json()["asset"]["status"] == "downloaded"
    assert downloaded.json()["asset"]["extracted_files"][0]["file_type"] == "ies"
    extracted_path = downloaded.json()["asset"]["extracted_files"][0]["relative_path"]

    extracted_file = client.get(
        f"/api/projects/{project_id}/luminaires/fixture1/photometry/extracted",
        params={"relative_path": extracted_path},
    )
    assert extracted_file.status_code == 200
    assert extracted_file.content == b"photometry"

    file_response = client.get(f"/api/projects/{project_id}/luminaires/fixture1/photometry/file")
    assert file_response.status_code == 200
    assert file_response.headers["content-type"].startswith("application/zip")

    removed = client.delete(
        f"/api/projects/{project_id}/luminaires/fixture1?expected_revision={selected.json()['revision']}"
    )
    assert removed.status_code == 200
    assert removed.json()["luminaires"] == []
    assert removed.json()["selected_luminaire_ids"] == []
    assert client.get(f"/api/projects/{project_id}/photometry").json()["assets"] == []
