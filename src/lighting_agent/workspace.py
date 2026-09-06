"""Project workspaces rooted in folders selected from the local desktop UI."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import WORKSPACE_REGISTRY_FILE
from .project_store import ProjectNotFoundError, ProjectStore
from .rag import EvidenceNotFoundError, LocalEvidenceStore
from .schemas import DesignBrief, ProjectState
from .storage import SQLiteDatabase


class WorkspaceError(ValueError):
    """A selected directory cannot be used as an independent workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    project_id: str
    directory: Path
    created_at: datetime
    updated_at: datetime


class WorkspaceRegistry:
    """Keep only local folder-to-project registrations outside project folders."""

    def __init__(self, database_path: Path = WORKSPACE_REGISTRY_FILE) -> None:
        self.database = SQLiteDatabase(database_path)
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    project_id TEXT PRIMARY KEY,
                    directory TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _record(row) -> WorkspaceRecord:
        return WorkspaceRecord(
            project_id=str(row["project_id"]),
            directory=Path(str(row["directory"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def get(self, project_id: str) -> WorkspaceRecord:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT project_id, directory, created_at, updated_at FROM workspaces WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
        return self._record(row)

    def find_by_directory(self, directory: Path) -> WorkspaceRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT project_id, directory, created_at, updated_at FROM workspaces WHERE directory = ?",
                (str(directory),),
            ).fetchone()
        finally:
            connection.close()
        return self._record(row) if row is not None else None

    def list(self) -> list[WorkspaceRecord]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT project_id, directory, created_at, updated_at FROM workspaces ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            connection.close()
        return [self._record(row) for row in rows]

    def register(self, project_id: str, directory: Path) -> WorkspaceRecord:
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workspaces (project_id, directory, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET directory = excluded.directory, updated_at = excluded.updated_at
                """,
                (project_id, str(directory), now.isoformat(), now.isoformat()),
            )
        return self.get(project_id)

    def remove(self, project_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM workspaces WHERE project_id = ?", (project_id,))


class WorkspaceProjectStore:
    """Route each project operation to its selected single-project directory."""

    def __init__(self, registry: WorkspaceRegistry | None = None) -> None:
        self.registry = registry or WorkspaceRegistry()

    @staticmethod
    def _directory(path: Path | str) -> Path:
        directory = Path(path).expanduser().resolve()
        if not directory.is_dir():
            raise WorkspaceError("所选项目文件夹不存在或不是目录")
        return directory

    def _store_for_directory(self, directory: Path) -> ProjectStore:
        database_path = directory / "lighting_design.sqlite3"
        if database_path.exists() and not self._is_workspace_database(database_path):
            raise WorkspaceError(
                "所选文件夹中的 lighting_design.sqlite3 不是可打开的照明项目，无法覆盖已有文件"
            )
        # A user-selected folder can contain arbitrary JSON project materials.
        # Never import them as legacy application state.
        return ProjectStore(directory, import_legacy=False)

    @staticmethod
    def _is_workspace_database(database_path: Path) -> bool:
        """Recognize only an existing application database without changing it."""

        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            try:
                row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False
        return row is not None

    def _store(self, project_id: str) -> ProjectStore:
        record = self.registry.get(project_id)
        if not record.directory.is_dir():
            raise ProjectNotFoundError(f"Project workspace is unavailable: {record.directory}")
        return self._store_for_directory(record.directory)

    def create_workspace(self, brief: DesignBrief, directory: Path | str) -> ProjectState:
        root = self._directory(directory)
        registered = self.registry.find_by_directory(root)
        if registered is not None:
            return self._store(registered.project_id).get(registered.project_id)

        store = self._store_for_directory(root)
        existing = store.list()
        if len(existing) > 1:
            raise WorkspaceError("所选文件夹包含多个项目，不能作为单个工作区")
        state = existing[0] if existing else store.create(brief)
        self.registry.register(state.project_id, root)
        return state

    def create(self, _brief: DesignBrief) -> ProjectState:
        raise WorkspaceError("请先在工作台选择项目文件夹后创建工作区")

    def get(self, project_id: str) -> ProjectState:
        return self._store(project_id).get(project_id)

    def list(self) -> list[ProjectState]:
        states: list[ProjectState] = []
        for record in self.registry.list():
            try:
                states.append(self._store(record.project_id).get(record.project_id))
            except (ProjectNotFoundError, OSError, WorkspaceError):
                continue
        return states

    def count(self) -> int:
        """Count registered projects without loading their JSON state."""

        count = 0
        for record in self.registry.list():
            database_path = record.directory / "lighting_design.sqlite3"
            if not record.directory.is_dir() or not self._is_workspace_database(database_path):
                continue
            uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
            try:
                connection = sqlite3.connect(uri, uri=True)
                try:
                    row = connection.execute(
                        "SELECT 1 FROM projects WHERE project_id = ?",
                        (record.project_id,),
                    ).fetchone()
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError):
                continue
            count += int(row is not None)
        return count

    def directory_for(self, project_id: str) -> Path:
        return self._store(project_id).directory

    def database_path_for(self, project_id: str) -> Path:
        return self._store(project_id).database_path

    def artifact_path(self, project_id: str, suffix: str) -> Path:
        return self._store(project_id).artifact_path(project_id, suffix)

    def delete(self, project_id: str) -> None:
        store = self._store(project_id)
        database_path = store.database_path
        store.delete(project_id)
        self.registry.remove(project_id)
        for target in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")):
            target.unlink(missing_ok=True)

    def __getattr__(self, name: str):
        """Delegate ordinary ProjectStore operations using their project_id argument."""

        def routed(project_id: str, *args, **kwargs):
            return getattr(self._store(project_id), name)(project_id, *args, **kwargs)

        return routed


class WorkspaceEvidenceStore:
    """Keep private project evidence in the selected workspace's SQLite file."""

    def __init__(self, global_store, projects: WorkspaceProjectStore) -> None:
        self.global_store = global_store
        self.projects = projects

    def _project_store(self, project_id: str) -> LocalEvidenceStore:
        database_path = self.projects.database_path_for(project_id)
        return LocalEvidenceStore(database_path=database_path, import_legacy=False)

    def add_document(self, document, *, source_type: str = "project_document", project_id: str | None = None) -> int:
        if project_id is None:
            return self.global_store.add_document(document, source_type=source_type)
        return self._project_store(project_id).add_document(
            document, source_type=source_type, project_id=project_id
        )

    def search(self, query: str, *, top_k: int = 3, project_id: str | None = None):
        global_results = self.global_store.search(query, top_k=top_k)
        if project_id is None:
            return global_results
        project_results = self._project_store(project_id).search(
            query, top_k=top_k, project_id=project_id
        )
        merged = {item.evidence_id: item for item in [*global_results, *project_results]}
        return sorted(
            merged.values(), key=lambda item: item.score if item.score is not None else 0, reverse=True
        )[:top_k]

    def get_evidence(self, evidence_ids: list[str], *, project_id: str | None = None):
        if project_id is None:
            return self.global_store.get_evidence(evidence_ids)
        resolved = []
        missing: list[str] = []
        project_store = self._project_store(project_id)
        for evidence_id in dict.fromkeys(evidence_ids):
            for store, scope in ((project_store, project_id), (self.global_store, None)):
                try:
                    resolved.extend(store.get_evidence([evidence_id], project_id=scope))
                    break
                except EvidenceNotFoundError:
                    continue
            else:
                missing.append(evidence_id)
        if missing:
            raise EvidenceNotFoundError(f"Evidence was not found: {', '.join(missing)}")
        return resolved

    def delete_project(self, project_id: str) -> int:
        return self._project_store(project_id).delete_project(project_id)

    def list_documents(self, *, project_id: str | None = None):
        if project_id is None:
            return self.global_store.list_documents()
        return self._project_store(project_id).list_documents(project_id=project_id)

    def get_document_chunks(self, source_hash: str, *, project_id: str | None = None):
        if project_id is None:
            return self.global_store.get_document_chunks(source_hash)
        return self._project_store(project_id).get_document_chunks(source_hash, project_id=project_id)

    def delete_document(self, source_hash: str):
        return self.global_store.delete_document(source_hash)
