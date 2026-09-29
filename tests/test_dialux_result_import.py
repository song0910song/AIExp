"""First-phase tests: simulation runs and DIALux result import."""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from lighting_agent import main as cli

from lighting_agent.dialux_results import DialuxVisionError
from lighting_agent.project_store import ProjectStore, RevisionConflictError
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import (
    CalculationInput,
    DesignBrief,
    DialuxVisionAnalysis,
    LuminaireCandidate,
    ProjectState,
    ProjectUpdate,
    SimulationMetrics,
    SimulationRun,
)
from lighting_agent.calculations import calculate_lumen_method
from lighting_agent.web_api import create_app


class FakeDialux:
    """No-op catalogue client: test luminaires never advertise photometry."""

    def search(self, _request: Any) -> list[LuminaireCandidate]:
        return []

    def download_photometry_zip(self, _detail_url: str) -> tuple[str, bytes]:
        raise AssertionError("test luminaires should not trigger photometry downloads")


def _vision_reading(
    _content: bytes,
    _media_type: str,
    *,
    illuminance_lx: float | None = 505,
    confidence: float = 0.96,
    is_dialux_result: bool = True,
) -> DialuxVisionAnalysis:
    return DialuxVisionAnalysis(
        is_dialux_result=is_dialux_result,
        maintained_illuminance_lx=illuminance_lx,
        confidence=confidence,
        metric_label="Maintained illuminance Em",
        calculation_surface="Working plane",
        explanation="DIALux result table clearly shows the maintained illuminance.",
        model="test-vision-model",
    )


def make_client(
    tmp_path,
    *,
    dialux_image_analyzer: Callable[[bytes, str], DialuxVisionAnalysis] = _vision_reading,
) -> TestClient:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=FakeDialux(),
        dialux_image_analyzer=dialux_image_analyzer,
    )
    return TestClient(app)


def _add_selected_luminaire(store: ProjectStore, project_id: str) -> ProjectState:
    """Save one final-selected luminaire for current and historical result tests."""
    candidate = LuminaireCandidate(
        luminaire_id="fixture-1",
        article_name="Fixture 1",
        detail_url="https://example.test/fixture-1",
        matching_status="matches",
    )
    saved, _, _ = store.append_luminaires(project_id, 0, [candidate])
    return store.set_selected_luminaires(project_id, saved.revision, ["fixture-1"])


def _matching_run(revision: int = 0) -> SimulationRun:
    return SimulationRun(
        kind="精算",
        status="succeeded",
        input_project_revision=revision,
        metrics=SimulationMetrics(
            maintained_illuminance_lx=750.0,
            minimum_illuminance_lx=450.0,
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
    assert "handoff_id" not in payload

    legacy = {
        **payload,
        "input_scene_revision": 3,
        "handoff_id": "handoff-0123456789abcdef",
        "input_snapshot_sha256": "a" * 64,
        "photometry_sha256_by_luminaire": {"fixture-1": "b" * 64},
    }
    legacy_restored = SimulationRun.model_validate(legacy)
    dumped = legacy_restored.model_dump(mode="json")
    assert "input_scene_revision" not in dumped
    assert "handoff_id" not in dumped
    assert "input_snapshot_sha256" not in dumped
    assert "photometry_sha256_by_luminaire" not in dumped


def test_store_append_simulation_run_records_revision(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(DesignBrief(project_name="仿真回灌"))
    run = _matching_run(revision=project.revision)

    updated = store.append_simulation_run(project.project_id, project.revision, run)

    assert updated.revision == 1
    assert updated.workflow_status == "simulation_pending"
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






def test_dialux_result_import_is_unverified(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "未验证证据"}).json()

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results",
        json={
            "expected_revision": 0,
            # Legacy handoff fields are ignored; the consistency check is removed.
            "handoff_id": "handoff-00000000000000000000000000000000",
            "metrics": {"maintained_illuminance_lx": 750.0},
        },
    )

    assert imported.status_code == 201
    run = imported.json()["simulation_run"]
    assert run["verification_status"] == "unverified"
    assert run["status"] == "unverified"
    assert "handoff_id" not in run


def test_dialux_result_import_conflict_on_stale_revision(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "并发冲突"}).json()
    project_id = project["project_id"]

    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project_id)
    # Advance the project revision before the import request arrives.
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
    client.post(
        f"/api/projects/{project_id}/dialux-results",
        json={
            "expected_revision": state.revision,
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


def test_dialux_image_upload_does_not_verify_new_design_by_itself(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post(
        "/api/projects",
        json={"project_name": "图片联合检验", "area_m2": 30, "target_illuminance_lx": 500},
    ).json()
    project_id = project["project_id"]
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project_id)
    calculation = calculate_lumen_method(
        CalculationInput(
            area_m2=30,
            target_illuminance_lx=500,
            luminaire_luminous_flux_lm=3200,
            luminaire_power_w=24,
            utilization_factor=0.6,
            maintenance_factor=0.8,
        )
    )
    state = store.update(
        project_id,
        ProjectUpdate(expected_revision=state.revision, calculations=[calculation]),
    )

    imported = client.post(
        f"/api/projects/{project_id}/dialux-results/upload",
        data={"expected_revision": str(state.revision)},
        files={"file": ("simulation.png", b"\x89PNG\r\n\x1a\nresult", "image/png")},
    )

    assert imported.status_code == 201
    payload = imported.json()
    assert payload["simulation_run"]["source_kind"] == "dialux_image"
    assert payload["simulation_run"]["metric_source"] == "vision"
    assert payload["simulation_run"]["metrics"]["maintained_illuminance_lx"] == 505
    assert payload["simulation_run"]["vision_analysis"]["confidence"] == 0.96
    assert payload["simulation_run"]["parser_version"] == "dialux-image-vision-1"
    assert payload["simulation_run"]["artifacts"][0]["file_name"] == "simulation.png"
    assert payload["simulation_run"]["verification_status"] == "unverified"
    assert payload["verification"]["overall_status"] == "pending"
    assert payload["verification"]["action"] == "rerun_dialux"
    assert payload["project"]["workflow_status"] == "simulation_pending"

    artifact = client.get(
        f"/api/projects/{project_id}/dialux-results/{payload['simulation_run']['run_id']}/artifact"
    )
    assert artifact.status_code == 200
    assert artifact.content == b"\x89PNG\r\n\x1a\nresult"


def test_dialux_image_upload_rejects_uncertain_vision_reading_without_manual_value(tmp_path) -> None:
    client = make_client(
        tmp_path,
        dialux_image_analyzer=lambda content, media_type: _vision_reading(
            content,
            media_type,
            illuminance_lx=None,
            confidence=0.35,
        ),
    )
    project = client.post("/api/projects", json={"project_name": "图片读数"}).json()
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project["project_id"])

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results/upload",
        data={"expected_revision": str(state.revision)},
        files={"file": ("simulation.png", b"\x89PNG\r\n\x1a\nresult", "image/png")},
    )

    assert imported.status_code == 422
    assert "视觉模型未能可靠识别" in imported.json()["detail"]


def test_dialux_image_manual_correction_does_not_bypass_vision_analysis(tmp_path) -> None:
    calls: list[tuple[bytes, str]] = []

    def analyze(content: bytes, media_type: str) -> DialuxVisionAnalysis:
        calls.append((content, media_type))
        return _vision_reading(content, media_type, illuminance_lx=480, confidence=0.92)

    client = make_client(tmp_path, dialux_image_analyzer=analyze)
    project = client.post("/api/projects", json={"project_name": "人工校正"}).json()
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project["project_id"])

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results/upload",
        data={"expected_revision": str(state.revision), "maintained_illuminance_lx": "500"},
        files={"file": ("simulation.png", b"\x89PNG\r\n\x1a\nresult", "image/png")},
    )

    assert imported.status_code == 201
    run = imported.json()["simulation_run"]
    assert calls == [(b"\x89PNG\r\n\x1a\nresult", "image/png")]
    assert run["metric_source"] == "manual"
    assert run["metrics"]["maintained_illuminance_lx"] == 500
    assert run["vision_analysis"]["maintained_illuminance_lx"] == 480


def test_dialux_image_upload_rejects_non_dialux_image(tmp_path) -> None:
    client = make_client(
        tmp_path,
        dialux_image_analyzer=lambda content, media_type: _vision_reading(
            content,
            media_type,
            illuminance_lx=None,
            confidence=0.98,
            is_dialux_result=False,
        ),
    )
    project = client.post("/api/projects", json={"project_name": "错误图片"}).json()
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project["project_id"])

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results/upload",
        data={"expected_revision": str(state.revision), "maintained_illuminance_lx": "500"},
        files={"file": ("not-dialux.png", b"\x89PNG\r\n\x1a\nresult", "image/png")},
    )

    assert imported.status_code == 422
    assert "无法确认该图片是 DIALux" in imported.json()["detail"]


def test_dialux_image_upload_reports_vision_service_failure_without_saving_file(tmp_path) -> None:
    def fail_analysis(_content: bytes, _media_type: str) -> DialuxVisionAnalysis:
        raise DialuxVisionError("视觉模型解析 DIALux 仿真图片失败")

    client = make_client(tmp_path, dialux_image_analyzer=fail_analysis)
    project = client.post("/api/projects", json={"project_name": "视觉服务失败"}).json()
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project["project_id"])

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results/upload",
        data={"expected_revision": str(state.revision), "maintained_illuminance_lx": "500"},
        files={"file": ("simulation.png", b"\x89PNG\r\n\x1a\nresult", "image/png")},
    )

    assert imported.status_code == 503
    assert "视觉模型解析" in imported.json()["detail"]
    result_directory = tmp_path / "projects" / f"{project['project_id']}.dialux-results"
    assert not list(result_directory.glob("*"))


def test_dialux_pdf_upload_extracts_labelled_illuminance(tmp_path) -> None:
    import fitz

    client = make_client(tmp_path)
    project = client.post(
        "/api/projects",
        json={"project_name": "报告自动读数", "target_illuminance_lx": 500},
    ).json()
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project["project_id"])
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Maintained average illuminance: 518 lx")
    pdf_bytes = pdf.tobytes()
    pdf.close()

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results/upload",
        data={"expected_revision": str(state.revision)},
        files={"file": ("dialux-report.pdf", pdf_bytes, "application/pdf")},
    )

    assert imported.status_code == 201
    run = imported.json()["simulation_run"]
    assert run["source_kind"] == "dialux_pdf"
    assert run["metrics"]["maintained_illuminance_lx"] == 518
    assert run["parser_version"] == "dialux-pdf-text-1"


def test_dialux_upload_rejects_spoofed_file_signature(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "文件校验"}).json()
    store = ProjectStore(tmp_path / "projects")
    state = _add_selected_luminaire(store, project["project_id"])

    imported = client.post(
        f"/api/projects/{project['project_id']}/dialux-results/upload",
        data={"expected_revision": str(state.revision), "maintained_illuminance_lx": "500"},
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
    )

    assert imported.status_code == 415


def test_cli_import_dialux_result_saves_unverified_evidence(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ProjectStore", lambda: ProjectStore(tmp_path))
    monkeypatch.setattr(cli, "create_evidence_store", lambda: LocalEvidenceStore(tmp_path / "index.json"))

    cli.main(["init-project", "CLI 结果回灌", "--area-m2", "30"])
    project = json.loads(capsys.readouterr().out)
    project_id = project["project_id"]

    store = ProjectStore(tmp_path)
    state = _add_selected_luminaire(store, project_id)

    cli.main(
        [
            "import-dialux-result",
            project_id,
            "--revision",
            str(state.revision),
            "--maintained-lx",
            "750",
        ]
    )
    response = json.loads(capsys.readouterr().out)

    assert response["simulation_run"]["verification_status"] == "unverified"
    assert response["simulation_run"]["status"] == "unverified"
    assert store.get(project_id).workflow_status == "simulation_pending"
