from __future__ import annotations

from fastapi.testclient import TestClient

import lighting_agent.web_api as web_api
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.web_api import create_app


def test_project_documents_are_scoped_and_deleted_with_project(tmp_path) -> None:
    project_store = ProjectStore(tmp_path / "projects")
    evidence_store = LocalEvidenceStore(tmp_path / "rag.json")
    client = TestClient(create_app(project_store=project_store, evidence_store=evidence_store))

    global_upload = client.post(
        "/api/documents",
        files={"file": ("national.md", b"National standard requires 300 lx.", "text/markdown")},
        data={"source_type": "standard"},
    )
    assert global_upload.status_code == 201

    project = client.post("/api/projects", json={"project_name": "Scoped project"}).json()
    project_id = project["project_id"]
    project_upload = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": ("brief.md", b"Project-only luminaire layout note.", "text/markdown")},
        data={"source_type": "project_document"},
    )
    assert project_upload.status_code == 201, project_upload.text
    documents_directory = project_store.directory / f"{project_id}.documents"
    assert documents_directory.exists()

    global_results = client.post("/api/evidence/search", json={"query": "project-only layout"}).json()["evidence"]
    assert all(item["source_name"] != project_upload.json()["source_name"] for item in global_results)
    scoped_results = client.post(
        "/api/evidence/search",
        json={"query": "project-only layout", "project_id": project_id},
    ).json()["evidence"]
    assert any(item["source_name"] == project_upload.json()["source_name"] for item in scoped_results)

    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert not documents_directory.exists()
    remaining_global = client.post("/api/evidence/search", json={"query": "national standard"}).json()["evidence"]
    assert any(item["source_name"] == global_upload.json()["source_name"] for item in remaining_global), remaining_global
    assert evidence_store.search("project-only layout", project_id=project_id) == []


def test_global_documents_can_be_listed_and_deleted(tmp_path, monkeypatch) -> None:
    project_store = ProjectStore(tmp_path / "projects")
    evidence_store = LocalEvidenceStore(tmp_path / "rag.json")
    global_directory = tmp_path / "global-docs"
    monkeypatch.setattr(web_api, "USER_DOCUMENTS_DIRECTORY", global_directory)
    client = TestClient(create_app(project_store=project_store, evidence_store=evidence_store))

    uploaded = client.post(
        "/api/documents",
        files={"file": ("standard.md", b"Global standard requires 500 lx.", "text/markdown")},
        data={"source_type": "standard"},
    )
    assert uploaded.status_code == 201
    payload = uploaded.json()
    stored_file = global_directory / payload["source_name"]
    listed = client.get("/api/documents")
    assert listed.status_code == 200
    assert any(item["source_hash"] == payload["sha256"] for item in listed.json())
    assert stored_file.exists()

    deleted = client.delete(f"/api/documents/{payload['sha256']}")
    assert deleted.status_code == 204
    assert not stored_file.exists()
    assert client.get("/api/documents").json() == []
    assert evidence_store.search("global standard") == []
    assert client.delete(f"/api/documents/{payload['sha256']}").status_code == 404
