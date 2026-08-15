from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.web_api import create_app


@pytest.mark.parametrize(
    ("kind", "suffix", "media_type"),
    [
        ("report", ".design-report.md", "text/markdown"),
        ("dialux-task", ".dialux-task.zip", "application/zip"),
    ],
)
def test_generated_deliverable_can_be_downloaded(tmp_path, kind: str, suffix: str, media_type: str) -> None:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "Download test"}).json()

    generated = client.post(
        f"/api/projects/{project['project_id']}/deliverables/{kind}?expected_revision=0"
    )
    assert generated.status_code == 200
    payload = generated.json()
    assert payload["filename"].endswith(suffix)

    downloaded = client.get(payload["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith(media_type)
    assert payload["filename"] in downloaded.headers["content-disposition"]
    assert downloaded.content
