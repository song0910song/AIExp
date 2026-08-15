from __future__ import annotations

import json

from fastapi.testclient import TestClient

from lighting_agent.document_loader import load_document
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import DesignBrief, ProjectState
from lighting_agent.web_api import ChatSessionStore, create_app


def test_project_store_imports_legacy_json_and_keeps_revision_history(tmp_path) -> None:
    legacy = ProjectState(brief=DesignBrief(project_name="Legacy project", area_m2=30))
    legacy_path = tmp_path / f"{legacy.project_id}.json"
    legacy_path.write_text(legacy.model_dump_json(), encoding="utf-8")

    store = ProjectStore(tmp_path)

    assert store.get(legacy.project_id).brief.project_name == "Legacy project"
    assert [state.revision for state in store.revisions(legacy.project_id)] == [0]
    assert legacy_path.exists()
    assert (tmp_path / "lighting_design.sqlite3").exists()


def test_rag_imports_legacy_json_with_stable_evidence_ids(tmp_path) -> None:
    legacy_index = tmp_path / "index.json"
    legacy_index.write_text(
        json.dumps(
            [
                {
                    "chunk_id": "legacy-evidence-1",
                    "source_name": "standard.md",
                    "source_type": "standard",
                    "source_hash": "legacy-source-hash",
                    "locator": "page 1",
                    "content": "Meeting room maintained illuminance is 500 lx.",
                    "indexed_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )

    store = LocalEvidenceStore(legacy_index)

    assert store.search("meeting room illuminance")[0].evidence_id == "legacy-evidence-1"
    assert store.get_evidence(["legacy-evidence-1"])[0].locator == "page 1"
    assert legacy_index.exists()


def test_evidence_adoption_is_saved_in_the_project_revision(tmp_path) -> None:
    source = tmp_path / "standard.md"
    source.write_text("Meeting room maintained illuminance is 500 lx.", encoding="utf-8")
    evidence_store = LocalEvidenceStore(tmp_path / "rag.json")
    evidence_store.add_document(load_document(source, allowed_root=tmp_path), source_type="standard")
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=evidence_store,
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "Evidence project"}).json()
    evidence = client.post("/api/evidence/search", json={"query": "meeting illuminance"}).json()["evidence"]

    response = client.post(
        f"/api/projects/{project['project_id']}/evidence",
        json={"expected_revision": 0, "evidence_ids": [evidence[0]["evidence_id"]]},
    )

    assert response.status_code == 200
    assert response.json()["project"]["revision"] == 1
    assert response.json()["project"]["evidence"][0]["evidence_id"] == evidence[0]["evidence_id"]
    history = client.get(f"/api/projects/{project['project_id']}/revisions")
    assert [item["revision"] for item in history.json()] == [0, 1]


def test_chat_sessions_survive_store_recreation(tmp_path) -> None:
    database_path = tmp_path / "lighting_design.sqlite3"
    first = ChatSessionStore(database_path)
    first.save("session-12345678", [{"role": "user", "content": "Continue the project"}])

    restored = ChatSessionStore(database_path).get("session-12345678")

    assert len(restored) == 1
    assert restored[0].content == "Continue the project"


def test_project_delete_removes_its_linked_chat_sessions(tmp_path) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.create(DesignBrief(project_name="Chat cleanup"))
    sessions = ChatSessionStore(store.database_path)
    sessions.save(
        "session-12345678",
        [{"role": "user", "content": "Project chat"}],
        project_id=project.project_id,
    )

    store.delete(project.project_id)

    assert sessions.get("session-12345678") == []
