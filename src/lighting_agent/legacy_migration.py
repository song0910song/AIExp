"""One-time import of projects created in user-selected workspaces."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path

from .config import LEGACY_WORKSPACE_REGISTRY_FILE
from .project_store import ProjectStore

LOGGER = logging.getLogger(__name__)


def migrate_legacy_workspaces(projects: ProjectStore) -> int:
    """Copy registered project data into the fixed projects directory."""

    registry_path = LEGACY_WORKSPACE_REGISTRY_FILE
    if not registry_path.is_file():
        return 0

    try:
        uri = f"file:{registry_path.resolve().as_posix()}?mode=ro"
        registry = sqlite3.connect(uri, uri=True)
        try:
            records = registry.execute("SELECT project_id, directory FROM workspaces").fetchall()
        finally:
            registry.close()
    except sqlite3.DatabaseError:
        LOGGER.exception("Could not read the legacy workspace registry")
        return 0

    migrated = 0
    for project_id, directory in records:
        project_id = str(project_id)
        source_root = Path(str(directory))
        source_database = source_root / "lighting_design.sqlite3"
        source_key = f"selected-workspace:{project_id}:{source_root.resolve()}"
        if not project_id.isalnum():
            continue
        if not source_database.is_file():
            LOGGER.warning("Skipped legacy project %s because its source database is unavailable", project_id)
            continue
        if projects.database.legacy_import_completed(source_key):
            continue

        try:
            source_uri = f"file:{source_database.resolve().as_posix()}?mode=ro"
            source = sqlite3.connect(source_uri, uri=True)
            source.row_factory = sqlite3.Row
            try:
                row = source.execute(
                    "SELECT project_id, revision, state_json, created_at, updated_at "
                    "FROM projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    continue
                target = projects.database.connect()
                try:
                    already_imported = target.execute(
                        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                    ).fetchone() is not None
                finally:
                    target.close()
                if not already_imported:
                    _copy_project_artifacts(source_root, projects.directory, project_id)
                    _import_project_database(projects, source, row, source_key, source_root)
                else:
                    with projects.database.transaction() as target:
                        target.execute(
                            "INSERT OR IGNORE INTO legacy_imports (source_key, imported_at) "
                            "VALUES (?, datetime('now'))",
                            (source_key,),
                        )
                migrated += 1
            finally:
                source.close()
        except (OSError, sqlite3.DatabaseError, ValueError):
            LOGGER.exception("Could not import legacy project %s", project_id)
    return migrated


def _copy_project_artifacts(source_root: Path, destination_root: Path, project_id: str) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    for source in source_root.glob(f"{project_id}.*"):
        if source.name == "lighting_design.sqlite3":
            continue
        destination = destination_root / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True, copy_function=_copy_missing)
        elif source.is_file() and not destination.exists():
            shutil.copy2(source, destination)


def _copy_missing(source: str, destination: str) -> str:
    target = Path(destination)
    if target.exists():
        return str(target)
    return str(shutil.copy2(source, destination))


def _import_project_database(
    projects: ProjectStore, source: sqlite3.Connection, row, source_key: str, source_root: Path
) -> None:
    project_id = str(row["project_id"])
    with projects.database.transaction() as destination:
        exists = destination.execute(
            "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if exists is None:
            destination.execute(
                "INSERT INTO projects (project_id, revision, state_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                tuple(row),
            )
            for revision in source.execute(
                "SELECT project_id, revision, state_json, event_type, created_at "
                "FROM project_revisions WHERE project_id = ? ORDER BY revision",
                (project_id,),
            ):
                destination.execute(
                    "INSERT OR IGNORE INTO project_revisions "
                    "(project_id, revision, state_json, event_type, created_at) VALUES (?, ?, ?, ?, ?)",
                    tuple(revision),
                )

            documents = source.execute(
                "SELECT source_hash, source_name, source_type, project_id, page_count, indexed_at "
                "FROM documents WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            for document in documents:
                destination.execute(
                    "INSERT OR IGNORE INTO documents "
                    "(source_hash, source_name, source_type, project_id, page_count, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    tuple(document),
                )
                for chunk in source.execute(
                    "SELECT chunk_id, source_hash, source_name, source_type, project_id, locator, content, indexed_at "
                    "FROM evidence_chunks WHERE source_hash = ?",
                    (document["source_hash"],),
                ):
                    destination.execute(
                        "INSERT OR IGNORE INTO evidence_chunks "
                        "(chunk_id, source_hash, source_name, source_type, project_id, locator, content, indexed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(chunk),
                    )
                artifact = source.execute(
                    "SELECT artifact_json FROM document_artifacts WHERE source_hash = ?",
                    (document["source_hash"],),
                ).fetchone()
                if artifact:
                    payload = json.loads(str(artifact["artifact_json"]))
                    source_path = Path(str(payload.get("source_path", "")))
                    try:
                        relative_path = source_path.resolve().relative_to(source_root.resolve())
                    except (OSError, ValueError):
                        relative_path = None
                    if relative_path is not None:
                        payload["source_path"] = str(projects.directory / relative_path)
                    destination.execute(
                        "INSERT OR IGNORE INTO document_artifacts (source_hash, artifact_json) VALUES (?, ?)",
                        (document["source_hash"], json.dumps(payload, ensure_ascii=False)),
                    )

            for standard in source.execute(
                "SELECT standard_id, source_hash, record_json FROM standard_versions "
                "WHERE source_hash IN (SELECT source_hash FROM documents WHERE project_id = ?)",
                (project_id,),
            ):
                destination.execute(
                    "INSERT OR IGNORE INTO standard_versions (standard_id, source_hash, record_json) VALUES (?, ?, ?)",
                    tuple(standard),
                )
            for session in source.execute(
                "SELECT session_id, project_id, messages_json, created_at, updated_at, expires_at "
                "FROM chat_sessions WHERE project_id = ?",
                (project_id,),
            ):
                destination.execute(
                    "INSERT OR IGNORE INTO chat_sessions "
                    "(session_id, project_id, messages_json, created_at, updated_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    tuple(session),
                )

        destination.execute(
            "INSERT OR IGNORE INTO legacy_imports (source_key, imported_at) "
            "VALUES (?, datetime('now'))",
            (source_key,),
        )
