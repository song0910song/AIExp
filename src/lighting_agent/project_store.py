"""SQLite project persistence with immutable revision snapshots."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .dialux_api import apply_brief_constraints, match_luminaire_candidate
from .config import DATABASE_FILE, PROJECTS_DIRECTORY, ensure_data_directories
from .schemas import (
    DesignBrief,
    FloorPlan,
    LuminaireCandidate,
    LuminaireSearchRequest,
    LuminaireBriefValidation,
    LuminaireSearchRun,
    ProjectState,
    ProjectUpdate,
    SimulationRun,
)
from .storage import SQLiteDatabase


class ProjectNotFoundError(FileNotFoundError):
    pass


class RevisionConflictError(RuntimeError):
    pass


def _brief_constraints(brief: DesignBrief) -> LuminaireSearchRequest:
    return apply_brief_constraints(LuminaireSearchRequest(keyword="project"), brief)


def _with_brief_validation(
    candidate: LuminaireCandidate, brief: DesignBrief, project_revision: int
) -> LuminaireCandidate:
    """Attach current-brief checks without replacing the search snapshot."""

    constraints = _brief_constraints(brief)
    checked = match_luminaire_candidate(candidate, constraints)
    validation = LuminaireBriefValidation(
        project_revision=project_revision,
        constraints=constraints,
        matching_status=checked.matching_status,
        missing_requested_fields=checked.missing_requested_fields,
        failed_requested_fields=checked.failed_requested_fields,
        criteria_checks=checked.criteria_checks,
    )
    return candidate.model_copy(update={"brief_validation": validation})


class ProjectStore:
    """Persist project state and every accepted revision in SQLite.

    ``directory`` remains the location for generated reports and the one-time
    importer for existing ``*.json`` project files. Passing it also creates an
    isolated database there, preserving the original test and CLI ergonomics.
    """

    def __init__(self, directory: Path | None = None, *, database_path: Path | None = None) -> None:
        ensure_data_directories()
        self.directory = directory or PROJECTS_DIRECTORY
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path or (
            DATABASE_FILE if directory is None else self.directory / "lighting_design.sqlite3"
        )
        self.database = SQLiteDatabase(self.database_path)
        self._import_legacy_projects()

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not project_id.isalnum():
            raise ValueError("project_id must be alphanumeric")

    def _path(self, project_id: str) -> Path:
        """Return the legacy JSON path for compatibility; state is no longer written there."""

        self._validate_project_id(project_id)
        return self.directory / f"{project_id}.json"

    def artifact_path(self, project_id: str, suffix: str) -> Path:
        self._validate_project_id(project_id)
        return self.directory / f"{project_id}{suffix}"

    def create(self, brief: DesignBrief) -> ProjectState:
        state = ProjectState(brief=brief)
        state.refresh_open_questions()
        state.refresh_workflow_status()
        payload = self._payload(state)
        timestamp = state.created_at.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects (project_id, revision, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (state.project_id, state.revision, payload, timestamp, state.updated_at.isoformat()),
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
        return ProjectState.model_validate_json(str(row["state_json"]))

    def update(self, project_id: str, update: ProjectUpdate) -> ProjectState:
        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision, state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            if int(row["revision"]) != update.expected_revision:
                raise RevisionConflictError(
                    f"Project revision is {row['revision']}, but update expected {update.expected_revision}"
                )
            state = ProjectState.model_validate_json(str(row["state_json"]))
            brief_changed = update.brief is not None and update.brief != state.brief
            selected_changed = (
                update.selected_luminaire_ids is not None
                and list(dict.fromkeys(update.selected_luminaire_ids)) != state.selected_luminaire_ids
            )
            floor_plan_changed = update.floor_plan is not None and update.floor_plan != state.floor_plan
            luminaires_changed = update.luminaires is not None and update.luminaires != state.luminaires
            for name in (
                "brief",
                "evidence",
                "calculations",
                "rule_checks",
                "luminaires",
                "luminaire_search_runs",
                "selected_luminaire_ids",
                "floor_plan",
                "simulation_runs",
                "open_questions",
            ):
                value = getattr(update, name)
                if value is not None:
                    setattr(state, name, value)
            if update.luminaires is not None:
                candidate_ids = {item.luminaire_id for item in state.luminaires}
                state.selected_luminaire_ids = [
                    item for item in state.selected_luminaire_ids if item in candidate_ids
                ]
            if update.brief is not None and update.open_questions is None:
                state.refresh_open_questions()
            if brief_changed:
                state.luminaires = [
                    _with_brief_validation(item, state.brief, state.revision + 1)
                    for item in state.luminaires
                ]
                # A changed task brief invalidates the prior final-selection conclusion.
                state.selected_luminaire_ids = []
            if brief_changed or selected_changed or floor_plan_changed or luminaires_changed:
                self._mark_simulation_runs_stale(
                    state,
                    self._simulation_stale_reason(
                        brief_changed=brief_changed,
                        selected_changed=selected_changed,
                        floor_plan_changed=floor_plan_changed,
                        luminaires_changed=luminaires_changed,
                    ),
                )
            state.refresh_workflow_status()
            state = ProjectState.model_validate(state.model_dump())
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            payload = self._payload(state)
            updated = connection.execute(
                """
                UPDATE projects
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE project_id = ? AND revision = ?
                """,
                (state.revision, payload, state.updated_at.isoformat(), project_id, update.expected_revision),
            )
            if updated.rowcount != 1:
                raise RevisionConflictError("Project was updated by another request; reload and retry")
            self._record_revision(connection, state, "update")
        return state

    @staticmethod
    def _simulation_stale_reason(
        *,
        brief_changed: bool,
        selected_changed: bool,
        floor_plan_changed: bool,
        luminaires_changed: bool,
    ) -> str:
        changes: list[str] = []
        if brief_changed:
            changes.append("design brief")
        if floor_plan_changed:
            changes.append("floor plan")
        if selected_changed:
            changes.append("selected luminaires")
        if luminaires_changed:
            changes.append("luminaire candidates")
        return "Project inputs changed: " + ", ".join(changes)

    @staticmethod
    def _mark_simulation_runs_stale(state: ProjectState, reason: str) -> None:
        """Invalidate prior simulation evidence without deleting its audit trail."""

        state.simulation_runs = [
            run.model_copy(
                update={
                    "status": "stale",
                    "verification_status": "stale",
                    "stale_reason": reason,
                }
            )
            if run.status != "stale" or run.verification_status != "stale"
            else run
            for run in state.simulation_runs
        ]

    def append_simulation_run(
        self,
        project_id: str,
        expected_revision: int,
        run: SimulationRun,
    ) -> ProjectState:
        """Append one imported or planned simulation run atomically."""

        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision, state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"Project revision is {row['revision']}, but update expected {expected_revision}"
                )
            state = ProjectState.model_validate_json(str(row["state_json"]))
            if run.input_project_revision != state.revision:
                raise ValueError("Simulation run input_project_revision must match the current project revision")
            state.simulation_runs = [*state.simulation_runs, run]
            state.refresh_workflow_status()
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            payload = self._payload(state)
            updated = connection.execute(
                """
                UPDATE projects
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE project_id = ? AND revision = ?
                """,
                (state.revision, payload, state.updated_at.isoformat(), project_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise RevisionConflictError("Project was updated by another request; reload and retry")
            self._record_revision(connection, state, "append_simulation_run")
        return state

    def get_simulation_run(self, project_id: str, run_id: str) -> SimulationRun:
        state = self.get(project_id)
        for run in state.simulation_runs:
            if run.run_id == run_id:
                return run
        raise ProjectNotFoundError(f"Simulation run {run_id!r} does not exist")

    def append_luminaires(
        self,
        project_id: str,
        expected_revision: int,
        candidates: list[LuminaireCandidate],
        search_run: LuminaireSearchRun | None = None,
    ) -> tuple[ProjectState, int, bool]:
        """Atomically add newly found luminaires without discarding project changes.

        Luminaire search saves are append-only: unlike a task-brief edit, a
        stale browser view cannot overwrite another user's change.  Rebasing
        this narrowly scoped operation on the latest snapshot makes repeated
        clicks and concurrent searches safe while keeping strict revision
        checks for all replacement-style project updates.

        Returns ``(state, saved_count, rebased)``.  A candidate is identified
        by its DIALux luminaire id, so retrying the same search is idempotent.
        """

        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision, state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")

            actual_revision = int(row["revision"])
            state = ProjectState.model_validate_json(str(row["state_json"]))
            existing_ids = {item.luminaire_id for item in state.luminaires}
            additions: list[LuminaireCandidate] = []
            for candidate in candidates:
                if candidate.matching_status == "rejected":
                    continue
                if candidate.luminaire_id in existing_ids:
                    continue
                existing_ids.add(candidate.luminaire_id)
                candidate = _with_brief_validation(candidate, state.brief, actual_revision + 1)
                # Keep every supplier result that passed the search request, even
                # when the stricter current project brief rejects it. The
                # validation is persisted for traceability and final selection
                # remains blocked below in set_selected_luminaires().
                additions.append(candidate)

            rebased = actual_revision != expected_revision
            if search_run is not None:
                search_run = search_run.model_copy(
                    update={"project_id": project_id, "project_revision": expected_revision}
                )
            if not additions and search_run is None:
                return state, 0, rebased

            state.luminaires = [*state.luminaires, *additions]
            if search_run is not None:
                state.luminaire_search_runs = [*state.luminaire_search_runs, search_run]
            state.refresh_workflow_status()
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            payload = self._payload(state)
            updated = connection.execute(
                """
                UPDATE projects
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE project_id = ? AND revision = ?
                """,
                (state.revision, payload, state.updated_at.isoformat(), project_id, actual_revision),
            )
            if updated.rowcount != 1:
                raise RevisionConflictError("Project was updated by another request; reload and retry")
            self._record_revision(connection, state, "append_luminaires")
        return state, len(additions), rebased

    def set_selected_luminaires(
        self,
        project_id: str,
        expected_revision: int,
        luminaire_ids: list[str],
    ) -> ProjectState:
        """Persist final luminaires, separately from the searchable candidate history."""

        self._validate_project_id(project_id)
        selected_ids = list(dict.fromkeys(luminaire_ids))
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision, state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"Project revision is {row['revision']}, but update expected {expected_revision}"
                )
            state = ProjectState.model_validate_json(str(row["state_json"]))
            validated = [
                _with_brief_validation(item, state.brief, state.revision)
                for item in state.luminaires
            ]
            state.luminaires = validated
            available = {item.luminaire_id: item for item in validated}
            available_ids = set(available)
            unknown_ids = [item for item in selected_ids if item not in available_ids]
            if unknown_ids:
                raise ValueError(
                    "Final luminaire selection contains unsaved candidates: " + ", ".join(unknown_ids)
                )
            ineligible_ids = [
                item for item in selected_ids
                if available[item].brief_validation is None
                or available[item].brief_validation.matching_status != "matches"
            ]
            if ineligible_ids:
                raise ValueError(
                    "Final luminaire selection requires candidates verified against the current brief: "
                    + ", ".join(ineligible_ids)
                )
            if state.selected_luminaire_ids == selected_ids:
                return state

            state.selected_luminaire_ids = selected_ids
            self._mark_simulation_runs_stale(state, "Project inputs changed: selected luminaires")
            state.refresh_workflow_status()
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            payload = self._payload(state)
            updated = connection.execute(
                """
                UPDATE projects
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE project_id = ? AND revision = ?
                """,
                (state.revision, payload, state.updated_at.isoformat(), project_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise RevisionConflictError("Project was updated by another request; reload and retry")
            self._record_revision(connection, state, "select_luminaires")
        return state

    def set_floor_plan(
        self,
        project_id: str,
        expected_revision: int,
        floor_plan: FloorPlan,
        area_candidate_index: int | None = None,
    ) -> ProjectState:
        """Persist a parsed drawing and apply one explicitly selected room boundary.

        The selected geometry is server-produced and hash-verified before this
        method is called. Applying it together with the plan preserves a
        single revision for every later calculation and luminaire search.
        """

        state = self.get(project_id)
        if state.revision != expected_revision:
            raise RevisionConflictError(
                f"Project revision is {state.revision}, but update expected {expected_revision}"
            )
        brief_updates: dict[str, object] = {}
        confirmed = set(state.brief.confirmed_fields)
        if area_candidate_index is not None:
            selected = floor_plan.area_candidates[area_candidate_index]
            if selected.area_m2 is None:
                raise ValueError("图纸单位无法换算为米，不能将该边界应用到任务书")
            brief_updates = {
                "area_m2": selected.area_m2,
                "length_m": selected.length_m,
                "width_m": selected.width_m,
                "confirmed_fields": confirmed | {"area_m2", "length_m", "width_m"},
            }
        if not state.brief.space_type and floor_plan.room_name:
            brief_updates["space_type"] = floor_plan.room_name
            brief_updates["confirmed_fields"] = set(brief_updates.get("confirmed_fields", confirmed)) | {"space_type"}
        stored_floor_plan = floor_plan.model_copy(
            update={"selected_area_candidate_index": area_candidate_index}
        )
        return self.update(
            project_id,
            ProjectUpdate(
                expected_revision=expected_revision,
                floor_plan=stored_floor_plan,
                brief=state.brief.model_copy(update=brief_updates) if brief_updates else None,
            ),
        )

    def revalidate_luminaires(self, project_id: str) -> tuple[ProjectState, int]:
        """Reapply the confirmed brief to saved candidates and record the repair as a revision."""

        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            state = ProjectState.model_validate_json(str(row["state_json"]))
            validated = [
                _with_brief_validation(item, state.brief, state.revision + 1)
                for item in state.luminaires
            ]
            changed = sum(before != after for before, after in zip(state.luminaires, validated, strict=True))
            if not changed:
                return state, 0
            state.luminaires = validated
            valid_ids = {
                item.luminaire_id
                for item in validated
                if item.brief_validation and item.brief_validation.matching_status == "matches"
            }
            state.selected_luminaire_ids = [
                item for item in state.selected_luminaire_ids if item in valid_ids
            ]
            state.refresh_workflow_status()
            state.revision += 1
            state.updated_at = datetime.now(UTC)
            payload = self._payload(state)
            connection.execute(
                """
                UPDATE projects
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE project_id = ? AND revision = ?
                """,
                (state.revision, payload, state.updated_at.isoformat(), project_id, state.revision - 1),
            )
            self._record_revision(connection, state, "revalidate_luminaires")
        return state, changed

    def delete(self, project_id: str) -> None:
        """Delete one project, its revision snapshots, chat sessions and generated files."""

        self._validate_project_id(project_id)
        with self.database.transaction() as connection:
            deleted = connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            if deleted.rowcount != 1:
                raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
            # Project revisions are removed by the foreign-key cascade. Chat
            # sessions use an indexed project id so they can expire or be
            # deleted independently of the conversation endpoint.
            connection.execute("DELETE FROM chat_sessions WHERE project_id = ?", (project_id,))

        for suffix in (
            ".design-report.md",
            ".design-report.json",
            ".dialux-task.zip",
            ".dialux-task.json",
            ".json",
        ):
            self.artifact_path(project_id, suffix).unlink(missing_ok=True)
        for directory in (
            self.directory / f"{project_id}.photometry",
            self.directory / f"{project_id}.plans",
        ):
            if directory.exists():
                shutil.rmtree(directory)

    def list(self) -> list[ProjectState]:
        connection = self.database.connect()
        try:
            rows = connection.execute("SELECT state_json FROM projects").fetchall()
        finally:
            connection.close()
        return [ProjectState.model_validate_json(str(row["state_json"])) for row in rows]

    def revisions(self, project_id: str) -> list[ProjectState]:
        """Return immutable project snapshots in revision order."""

        self._validate_project_id(project_id)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT state_json FROM project_revisions
                WHERE project_id = ? ORDER BY revision
                """,
                (project_id,),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            raise ProjectNotFoundError(f"Project {project_id!r} does not exist")
        return [ProjectState.model_validate_json(str(row["state_json"])) for row in rows]

    def _record_revision(self, connection, state: ProjectState, event_type: str) -> None:
        connection.execute(
            """
            INSERT INTO project_revisions (project_id, revision, state_json, event_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (state.project_id, state.revision, self._payload(state), event_type, state.updated_at.isoformat()),
        )

    @staticmethod
    def _payload(state: ProjectState) -> str:
        return json.dumps(state.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))

    def _import_legacy_projects(self) -> None:
        source_key = f"projects-json:{self.directory.resolve()}"
        if self.database.legacy_import_completed(source_key):
            return
        states: list[ProjectState] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                states.append(ProjectState.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                # Generated DIALux packages and reports are not project states.
                if path.stem.isalnum():
                    raise RuntimeError(f"Cannot import legacy project file: {path}") from None
        with self.database.transaction() as connection:
            for state in states:
                exists = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (state.project_id,)
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    """
                    INSERT INTO projects (project_id, revision, state_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        state.project_id,
                        state.revision,
                        self._payload(state),
                        state.created_at.isoformat(),
                        state.updated_at.isoformat(),
                    ),
                )
                self._record_revision(connection, state, "legacy_import")
            connection.execute(
                "INSERT INTO legacy_imports (source_key, imported_at) VALUES (?, ?)",
                (source_key, datetime.now(UTC).isoformat()),
            )
