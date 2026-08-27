from __future__ import annotations

from fastapi.testclient import TestClient

from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.web_api import create_app
from lighting_agent.workspace import WorkspaceProjectStore, WorkspaceRegistry


def make_client(tmp_path, directory_picker) -> TestClient:
    projects = WorkspaceProjectStore(WorkspaceRegistry(tmp_path / "workspace-registry.sqlite3"))
    return TestClient(
        create_app(
            project_store=projects,
            evidence_store=LocalEvidenceStore(
                database_path=tmp_path / "global-evidence.sqlite3",
                import_legacy=False,
            ),
            directory_picker=directory_picker,
        )
    )


def select_directory(client: TestClient) -> dict:
    selected = client.post("/api/workspaces/select-directory")
    assert selected.status_code == 200, selected.text
    payload = selected.json()
    assert payload["selected"] is True
    return payload


def test_workspace_creation_uses_selected_nonempty_directory_for_project_files(tmp_path) -> None:
    workspace = tmp_path / "existing-project-folder"
    workspace.mkdir()
    original_file = workspace / "client-reference.txt"
    original_file.write_text("keep this file", encoding="utf-8")
    client = make_client(tmp_path, lambda: workspace)

    selection = select_directory(client)
    created = client.post(
        "/api/projects",
        json={
            "project_name": "Selected workspace",
            "workspace_selection_id": selection["selection_id"],
        },
    )

    assert created.status_code == 201, created.text
    project = created.json()
    project_id = project["project_id"]
    assert selection["directory"] == str(workspace.resolve())
    assert (workspace / "lighting_design.sqlite3").exists()
    assert original_file.read_text(encoding="utf-8") == "keep this file"

    uploaded = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": ("brief.md", b"Fixture mounting point: 2.7 m", "text/markdown")},
        data={"source_type": "project_document"},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert (workspace / f"{project_id}.documents" / "brief.md").exists()

    # Project-only evidence is durable inside the selected workspace database.
    searched = client.post(
        "/api/evidence/search",
        json={"query": "mounting point", "project_id": project_id},
    )
    assert searched.status_code == 200
    assert any(item["source_name"] == "brief.md" for item in searched.json()["evidence"])


def test_selecting_an_existing_workspace_reopens_its_single_project(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = make_client(tmp_path, lambda: workspace)

    first = select_directory(client)
    created = client.post(
        "/api/projects",
        json={"project_name": "Original project", "workspace_selection_id": first["selection_id"]},
    )
    assert created.status_code == 201
    original = created.json()

    second = select_directory(client)
    reopened = client.post(
        "/api/projects",
        json={"project_name": "Should not overwrite", "workspace_selection_id": second["selection_id"]},
    )
    assert reopened.status_code == 201
    assert reopened.json()["project_id"] == original["project_id"]
    assert reopened.json()["brief"]["project_name"] == "Original project"
    assert len(client.get("/api/projects").json()) == 1


def test_workspace_creation_requires_native_directory_selection(tmp_path) -> None:
    client = make_client(tmp_path, lambda: tmp_path)

    created = client.post("/api/projects", json={"project_name": "No directory"})

    assert created.status_code == 422
    assert created.json()["detail"] == "请先选择项目文件夹"


def test_workspace_directory_picker_can_be_cancelled(tmp_path) -> None:
    client = make_client(tmp_path, lambda: None)

    response = client.post("/api/workspaces/select-directory")

    assert response.status_code == 200
    assert response.json() == {"selected": False}


def test_workspace_rejects_an_unrelated_database_without_modifying_it(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = workspace / "lighting_design.sqlite3"
    original = b"not a SQLite database"
    database.write_bytes(original)
    client = make_client(tmp_path, lambda: workspace)

    selection = select_directory(client)
    created = client.post(
        "/api/projects",
        json={"project_name": "Collision", "workspace_selection_id": selection["selection_id"]},
    )

    assert created.status_code == 422
    assert "无法覆盖已有文件" in created.json()["detail"]
    assert database.read_bytes() == original


def test_workspace_delete_preserves_user_files_and_unregisters_directory(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_file = workspace / "source.dxf"
    source_file.write_text("client-owned", encoding="utf-8")
    client = make_client(tmp_path, lambda: workspace)

    selection = select_directory(client)
    project = client.post(
        "/api/projects",
        json={"project_name": "Deletable", "workspace_selection_id": selection["selection_id"]},
    ).json()

    deleted = client.delete(f"/api/projects/{project['project_id']}")

    assert deleted.status_code == 204
    assert source_file.read_text(encoding="utf-8") == "client-owned"
    assert not (workspace / "lighting_design.sqlite3").exists()
    assert client.get("/api/projects").json() == []
