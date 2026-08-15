"""First-phase tests: simulation runs, handoff identity and DIALux result import."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from lighting_agent import main as cli

from lighting_agent.deliverables import (
    build_dialux_task_archive,
    build_dialux_task_package,
    read_dialux_task_package,
)
from lighting_agent.photometry_assets import PhotometryAssetStore
from lighting_agent.project_store import ProjectStore, RevisionConflictError
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import (
    DesignBrief,
    LuminaireCandidate,
    ProjectState,
    ProjectUpdate,
    SimulationMetrics,
    SimulationRun,
)
from lighting_agent.web_api import create_app


class FakeDialux:
    """No-op catalogue client: test luminaires never advertise photometry."""

    def search(self, _request: Any) -> list[LuminaireCandidate]:
        return []

    def download_photometry_zip(self, _detail_url: str) -> tuple[str, bytes]:
        raise AssertionError("test luminaires should not trigger photometry downloads")


def make_client(tmp_path) -> TestClient:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=FakeDialux(),
    )
    return TestClient(app)


def _add_selected_luminaire(store: ProjectStore, project_id: str) -> ProjectState:
    """Save one final-selected luminaire so a DIALux handoff can be generated."""
    candidate = LuminaireCandidate(
        luminaire_id="fixture-1",
        article_name="Fixture 1",
        detail_url="https://example.test/fixture-1",
        matching_status="matches",
    )
    saved, _, _ = store.append_luminaires(project_id, 0, [candidate])
    return store.set_selected_luminaires(project_id, saved.revision, ["fixture-1"])


def _generate_handoff(client: TestClient, project_id: str, revision: int) -> dict:
    generated = client.post(
        f"/api/projects/{project_id}/deliverables/dialux-task",
        params={"expected_revision": revision},
    )
    assert generated.status_code == 200
    return generated.json()


def _matching_run(revision: int = 0) -> SimulationRun:
    return SimulationRun(
        kind="精算",
        status="succeeded",
        input_project_revision=revision,
        metrics=SimulationMetrics(
            maintained_illuminance_lx=750.0,
            minimum_illuminance_lx=450.0,
            uniformity_u0=0.60,
            ugr=19.0,
            installed_power_density_w_m2=9.0,
        ),
        verification_status="matched",
        source_kind="manual_form",
        parser_version="test-1",
        completed_at=None,
    )


def test_simulation_run_json_round_trip_and_legacy_drop() -> None:
    run = _matching_run()
    payload = run.model_dump(mode="json")
    restored = SimulationRun.model_validate(payload)
    assert restored.run_id == run.run_id
    assert restored.verification_status == "matched"
    assert restored.metrics is not None
    assert restored.metrics.uniformity_u0 == 0.60

    legacy = {**payload, "input_scene_revision": 3}
    legacy_restored = SimulationRun.model_validate(legacy)
    assert "input_scene_revision" not in legacy_restored.model_dump(mode="json")


def test_store_append_simulation_run_records_revision(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(DesignBrief(project_name="仿真回灌"))
    run = _matching_run(revision=project.revision)

    updated = store.append_simulation_run(project.project_id, project.revision, run)

    assert updated.revision == 1
    assert updated.workflow_status == "simulation_verified"
    assert [item.revision for item in store.revisions(project.project_id)] == [0, 1]
    fetched = store.get_simulation_run(project.project_id, run.run_id)
    assert fetched.run_id == run.run_id


def test_store_simulation_run_must_match_project_revision(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(DesignBrief(project_name="版本校验"))
    run = _matching_run(revision=5)
    with pytest.raises(ValueError, match="must match the current project revision"):
        store.append_simulation_run(project.project_id, project.revision, run)


def test_brief_change_marks_simulation_stale(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(DesignBrief(project_name="任务书变更"))
    store.append_simulation_run(project.project_id, project.revision, _matching_run())

    changed = store.update(
        project.project_id,
        ProjectUpdate(
            expected_revision=1,
            brief=project.brief.model_copy(update={"space_type": "会议室"}),
        ),
    )

    latest = changed.simulation_runs[-1]
    assert latest.status == "stale"
    assert latest.verification_status == "stale"
    assert "design brief" in (latest.stale_reason or "")
    assert changed.workflow_status == "needs_revision"


def test_selected_luminaire_change_marks_simulation_stale(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(DesignBrief(project_name="最终灯具变更"))
    first = LuminaireCandidate(
        luminaire_id="fixture-a",
        article_name="Fixture A",
        detail_url="https://example.test/a",
    )
    saved, count, _ = store.append_luminaires(project.project_id, project.revision, [first])
    assert count == 1
    store.set_selected_luminaires(project.project_id, saved.revision, ["fixture-a"])
    after_selection = store.get(project.project_id)
    store.append_simulation_run(project.project_id, after_selection.revision, _matching_run(after_selection.revision))

    changed = store.set_selected_luminaires(project.project_id, after_selection.revision + 1, [])

    assert changed.selected_luminaire_ids == []
    latest = changed.simulation_runs[-1]
    assert latest.status == "stale"
    assert changed.workflow_status == "needs_revision"


def test_handoff_package_has_stable_identity() -> None:
    state = __import__("lighting_agent.schemas", fromlist=["ProjectState"]).ProjectState(
        brief=DesignBrief(project_name="交接身份", area_m2=30),
    )
    package = build_dialux_task_package(state)

    assert package["handoff_id"].startswith("handoff-")
    assert len(package["input_snapshot_sha256"]) == 64
    assert package["input_snapshot"]["project_id"] == state.project_id
    assert package["input_snapshot"]["project_revision"] == state.revision
    # The handoff id is derived from the snapshot, so identical inputs stay stable.
    assert build_dialux_task_package(state)["handoff_id"] == package["handoff_id"]


def test_handoff_archive_embeds_identity_readable_back(tmp_path) -> None:
    state = __import__("lighting_agent.schemas", fromlist=["ProjectState"]).ProjectState(
        brief=DesignBrief(project_name="交接归档"),
        luminaires=[
            LuminaireCandidate(
                luminaire_id="fixture-1",
                article_name="Fixture 1",
                detail_url="https://example.test/fixture-1",
            )
        ],
        selected_luminaire_ids=["fixture-1"],
    )
    assets = PhotometryAssetStore(tmp_path / "projects", downloader=object())
    archive_bytes = build_dialux_task_archive(state, assets)

    package = read_dialux_task_package(archive_bytes)

    assert package["handoff_id"].startswith("handoff-")
    assert len(package["input_snapshot_sha256"]) == 64
    assert package["project_id"] == state.project_id


def _read_handoff_package(tmp_path, project_id: str) -> dict:
    zip_path = tmp_path / "projects" / f"{project_id}.dialux-task.zip"
    with ZipFile(zip_path) as archive:
        return json.loads(archive.read("dialux-task.json"))


def test_dialux_result_import_matches_current_handoff(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "结果回灌"}).json()
    project_id = project["project_id"]

    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project_id)
    _generate_handoff(client, project_id, state.revision)
    package = _read_handoff_package(tmp_path, project_id)

    imported = client.post(
        f"/api/projects/{project_id}/dialux-results",
        json={
            "expected_revision": state.revision,
            "handoff_id": package["handoff_id"],
            "input_snapshot_sha256": package["input_snapshot_sha256"],
            "source_kind": "manual_form",
            "metrics": {
                "maintained_illuminance_lx": 750.0,
                "minimum_illuminance_lx": 450.0,
                "uniformity_u0": 0.60,
                "ugr": 19.0,
                "installed_power_density_w_m2": 9.0,
            },
        },
    )

    assert imported.status_code == 201
    payload = imported.json()
    run = payload["simulation_run"]
    assert run["verification_status"] == "matched"
    assert run["status"] == "succeeded"
    assert run["handoff_id"] == package["handoff_id"]
    assert payload["project"]["workflow_status"] == "simulation_verified"
    assert payload["project"]["revision"] == state.revision + 1


def test_dialux_result_import_requires_handoff_first(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "无交接包"}).json()

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results",
        json={
            "expected_revision": 0,
            "handoff_id": "handoff-00000000000000000000000000000000",
            "metrics": {"maintained_illuminance_lx": 750.0},
        },
    )

    assert imported.status_code == 404


def test_dialux_result_import_mismatch_is_not_matched(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "不匹配回灌"}).json()
    project_id = project["project_id"]

    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project_id)
    _generate_handoff(client, project_id, state.revision)

    imported = client.post(
        f"/api/projects/{project_id}/dialux-results",
        json={
            "expected_revision": state.revision,
            "handoff_id": "handoff-00000000000000000000000000000000",
            "metrics": {"maintained_illuminance_lx": 750.0},
        },
    )

    assert imported.status_code == 201
    payload = imported.json()
    run = payload["simulation_run"]
    assert run["verification_status"] == "mismatch"
    assert run["status"] == "unverified"
    assert run["verification_messages"]
    assert payload["project"]["workflow_status"] == "needs_revision"


def test_dialux_result_import_conflict_on_stale_revision(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "并发冲突"}).json()
    project_id = project["project_id"]

    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project_id)
    _generate_handoff(client, project_id, state.revision)
    # Advance the project revision after the handoff was generated.
    store.update(
        project_id,
        ProjectUpdate(
            expected_revision=state.revision,
            brief=store.get(project_id).brief.model_copy(update={"space_type": "会议室"}),
        ),
    )

    imported = client.post(
        f"/api/projects/{project_id}/dialux-results",
        json={
            "expected_revision": state.revision,
            "handoff_id": "handoff-00000000000000000000000000000000",
            "metrics": {"maintained_illuminance_lx": 750.0},
        },
    )

    assert imported.status_code == 409


def test_dialux_result_list_and_get(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "结果查询"}).json()
    project_id = project["project_id"]

    listed = client.get(f"/api/projects/{project_id}/dialux-results")
    assert listed.status_code == 200
    assert listed.json() == []

    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project_id)
    _generate_handoff(client, project_id, state.revision)
    package = _read_handoff_package(tmp_path, project_id)
    client.post(
        f"/api/projects/{project_id}/dialux-results",
        json={
            "expected_revision": state.revision,
            "handoff_id": package["handoff_id"],
            "metrics": {"maintained_illuminance_lx": 750.0},
        },
    )

    listed = client.get(f"/api/projects/{project_id}/dialux-results")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    run_id = listed.json()[0]["run_id"]
    detail = client.get(f"/api/projects/{project_id}/dialux-results/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id


def test_cli_import_dialux_result_verifies_handoff(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ProjectStore", lambda: ProjectStore(tmp_path))
    monkeypatch.setattr(cli, "create_evidence_store", lambda: LocalEvidenceStore(tmp_path / "index.json"))

    cli.main(["init-project", "CLI 结果回灌", "--area-m2", "30"])
    project = json.loads(capsys.readouterr().out)
    project_id = project["project_id"]

    store = ProjectStore(tmp_path)
    state = _add_selected_luminaire(store, project_id)
    cli.main(["create-dialux-task", project_id, "--revision", str(state.revision)])
    capsys.readouterr()

    zip_path = tmp_path / f"{project_id}.dialux-task.zip"
    with ZipFile(zip_path) as archive:
        package = json.loads(archive.read("dialux-task.json"))

    cli.main(
        [
            "import-dialux-result",
            project_id,
            "--revision",
            str(state.revision),
            "--handoff-id",
            package["handoff_id"],
            "--maintained-lx",
            "750",
            "--uniformity-u0",
            "0.6",
        ]
    )
    response = json.loads(capsys.readouterr().out)

    assert response["simulation_run"]["verification_status"] == "matched"
    assert response["simulation_run"]["status"] == "succeeded"
    assert response["verification_messages"] == []
    assert store.get(project_id).workflow_status == "simulation_verified"
