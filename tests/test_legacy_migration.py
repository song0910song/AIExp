from __future__ import annotations

import sqlite3

from lighting_agent import legacy_migration
from lighting_agent.document_loader import load_document
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import DesignBrief


def test_registered_project_and_private_materials_migrate_once(tmp_path, monkeypatch):
    project_id = "legacyproject01"
    source_root = tmp_path / "selected" / project_id
    old_store = ProjectStore(source_root, import_legacy=False)
    old_store.create(DesignBrief(project_name="Legacy"), project_id=project_id)
    source_file = source_root / f"{project_id}.documents" / "brief.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("Legacy project material", encoding="utf-8")
    old_evidence = LocalEvidenceStore(database_path=old_store.database_path)
    old_evidence.add_document(load_document(source_file, allowed_root=source_root), project_id=project_id)

    registry_path = tmp_path / "workspace_registry.sqlite3"
    registry = sqlite3.connect(registry_path)
    registry.execute(
        "CREATE TABLE workspaces (project_id TEXT PRIMARY KEY, directory TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    registry.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
        (project_id, str(source_root), "created", "updated"),
    )
    registry.commit()
    registry.close()
    monkeypatch.setattr(legacy_migration, "LEGACY_WORKSPACE_REGISTRY_FILE", registry_path)

    target = ProjectStore(
        tmp_path / "data" / "projects",
        database_path=tmp_path / "data" / "lighting_design.sqlite3",
    )
    assert legacy_migration.migrate_legacy_workspaces(target) == 1
    assert target.get(project_id).brief.project_name == "Legacy"
    migrated_file = target.directory / f"{project_id}.documents" / "brief.md"
    assert migrated_file.read_text(encoding="utf-8") == "Legacy project material"

    migrated_evidence = LocalEvidenceStore(database_path=target.database_path)
    documents = migrated_evidence.list_documents(project_id=project_id)
    assert len(documents) == 1
    artifact = migrated_evidence.get_document_artifact(documents[0].source_hash, project_id=project_id)
    assert artifact["source_path"] == str(migrated_file)
    assert legacy_migration.migrate_legacy_workspaces(target) == 0
