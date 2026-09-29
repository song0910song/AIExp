"""Versioned SQLite persistence for CAD plans and luminaire candidates."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .config import DATABASE_FILE, PROJECTS_DIRECTORY, ensure_data_directories
from .schemas import DesignBrief, FloorPlan, LuminaireCandidate, LuminaireSearchRun, ProjectState, ProjectUpdate
from .storage import SQLiteDatabase


class ProjectNotFoundError(FileNotFoundError):
    pass


class RevisionConflictError(RuntimeError):
    pass


class ProjectStore:
    def __init__(
        self, directory: Path | None = None, *, database_path: Path | None = None, import_legacy: bool = True
    ) -> None:
        ensure_data_directories()
        self.directory = directory or PROJECTS_DIRECTORY
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path or (
            DATABASE_FILE if directory is None else self.directory / "lighting_design.sqlite3"
        )
        self.database = SQLiteDatabase(self.database_path)
        if import_legacy:
            self._import_legacy_projects()

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not project_id.isalnum():
            raise ValueError("Invalid project ID")

    def artifact_path(self, project_id: str, suffix: str) -> Path:
        self._validate_project_id(project_id)
        return self.directory / f"{project_id}{suffix}"

    def create(self, brief: DesignBrief, *, project_id: str | None = None) -> ProjectState:
        if project_id:
            self._validate_project_id(project_id)
        state = ProjectState(brief=brief, **({"project_id": project_id} if project_id else {}))
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO projects (project_id, revision, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (state.project_id, 0, self._payload(state), state.created_at.isoformat(), state.updated_at.isoformat()),
            )
            self._record_revision(connection, state, "create")
        return state

    def get(self, project_id: str) -> ProjectState:
        self._validate_project_id(project_id)
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
        return ProjectState.model_validate_json(row["state_json"])

    def update(self, project_id: str, update: ProjectUpdate) -> ProjectState:
        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision, state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            if row["revision"] != update.expected_revision:
                raise RevisionConflictError(
                    f"Project revision is {row['revision']}, expected {update.expected_revision}"
                )
            state = ProjectState.model_validate_json(row["state_json"])
            changes = {
                name: getattr(update, name)
                for name in ("brief", "floor_plan", "luminaires", "luminaire_search_runs")
                if getattr(update, name) is not None
            }
            state = state.model_copy(update=changes)
            state = ProjectState.model_validate(state.model_dump(mode="json"))
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            connection.execute(
                "UPDATE projects SET revision = ?, state_json = ?, updated_at = ? WHERE project_id = ?",
                (state.revision, self._payload(state), state.updated_at.isoformat(), project_id),
            )
            self._record_revision(connection, state, "update")
        return state

    def set_floor_plan(
        self, project_id: str, expected_revision: int, plan: FloorPlan, area_candidate_index: int | None = None
    ) -> ProjectState:
        state = self.get(project_id)
        if area_candidate_index is None:
            stored = plan.model_copy(update={"selected_area_candidate_index": None})
            return self.update(project_id, ProjectUpdate(expected_revision=expected_revision, floor_plan=stored))
        if area_candidate_index < 0 or area_candidate_index >= len(plan.area_candidates):
            raise ValueError("房间边界候选不存在")
        candidate = plan.area_candidates[area_candidate_index]
        if candidate.area_m2 is None:
            raise ValueError("图纸单位无法换算为米，无法确认面积")
        brief = state.brief.model_copy(update={
            "area_m2": candidate.area_m2,
            "length_m": candidate.length_m,
            "width_m": candidate.width_m,
            "confirmed_fields": state.brief.confirmed_fields | {"area_m2", "length_m", "width_m"},
        })
        if not brief.space_type and plan.room_name:
            brief = brief.model_copy(update={
                "space_type": plan.room_name,
                "confirmed_fields": brief.confirmed_fields | {"space_type"},
            })
        stored = plan.model_copy(update={"selected_area_candidate_index": area_candidate_index})
        return self.update(
            project_id, ProjectUpdate(expected_revision=expected_revision, brief=brief, floor_plan=stored)
        )

    def append_luminaires(
        self,
        project_id: str,
        expected_revision: int,
        candidates: list[LuminaireCandidate],
        search_run: LuminaireSearchRun | None = None,
    ) -> tuple[ProjectState, int, bool]:
        # Append-only search tolerates a stale browser revision; other edits remain locked.
        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision, state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            state = ProjectState.model_validate_json(row["state_json"])
            existing = {item.luminaire_id for item in state.luminaires}
            additions: list[LuminaireCandidate] = []
            for candidate in candidates:
                if candidate.matching_status != "rejected" and candidate.luminaire_id not in existing:
                    additions.append(candidate)
                    existing.add(candidate.luminaire_id)
            if not additions and search_run is None:
                return state, 0, row["revision"] != expected_revision
            state.luminaires.extend(additions)
            if search_run:
                state.luminaire_search_runs.append(search_run)
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            connection.execute(
                "UPDATE projects SET revision = ?, state_json = ?, updated_at = ? WHERE project_id = ?",
                (state.revision, self._payload(state), state.updated_at.isoformat(), project_id),
            )
            self._record_revision(connection, state, "search_luminaires")
        return state, len(additions), row["revision"] != expected_revision

    def list(self) -> list[ProjectState]:
        connection = self.database.connect()
        try:
            rows = connection.execute("SELECT state_json FROM projects").fetchall()
        finally:
            connection.close()
        return [ProjectState.model_validate_json(row["state_json"]) for row in rows]

    def count(self) -> int:
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT COUNT(*) FROM projects").fetchone()
        finally:
            connection.close()
        return row[0]

    def delete(self, project_id: str) -> None:
        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            if row.rowcount != 1:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            connection.execute("DELETE FROM chat_sessions WHERE project_id = ?", (project_id,))
        for path in self.directory.glob(f"{project_id}.*"):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.suffix == ".json":
                path.unlink()

    def revisions(self, project_id: str) -> list[ProjectState]:
        self._validate_project_id(project_id)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT state_json FROM project_revisions WHERE project_id = ? ORDER BY revision",
                (project_id,),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
        return [ProjectState.model_validate_json(row["state_json"]) for row in rows]

    def _record_revision(self, connection, state: ProjectState, event_type: str) -> None:
        connection.execute(
            "INSERT INTO project_revisions (project_id, revision, state_json, event_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (state.project_id, state.revision, self._payload(state), event_type, state.updated_at.isoformat()),
        )

    @staticmethod
    def _payload(state: ProjectState) -> str:
        return json.dumps(state.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))

    def _import_legacy_projects(self) -> None:
        source_key = f"projects-json:{self.directory.resolve()}"
        if self.database.legacy_import_completed(source_key):
            return
        with self.database.transaction() as connection:
            for path in sorted(self.directory.glob("*.json")):
                try:
                    state = ProjectState.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (state.project_id,)
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    "INSERT INTO projects (project_id, revision, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        state.project_id, state.revision, self._payload(state),
                        state.created_at.isoformat(), state.updated_at.isoformat(),
                    ),
                )
                self._record_revision(connection, state, "legacy_import")
            connection.execute(
                "INSERT OR IGNORE INTO legacy_imports (source_key, imported_at) VALUES (?, ?)",
                (source_key, datetime.now(UTC).isoformat()),
            )
