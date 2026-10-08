from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient

from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import (
    DesignBrief,
    DialuxLayoutItem,
    DialuxOptimizationRequest,
    DialuxRunRecord,
    DialuxRunRequest,
    FloorPlan,
    FloorPlanAsset,
    ProjectState,
    SpatialModel,
)
from lighting_agent.dialux_service import DialuxExecutionService
from lighting_agent.cad_auto_analysis import apply_model_analysis
from lighting_agent.web_api import create_app


def _drawing() -> bytes:
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = 6
    document.modelspace().add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True,
                                        dxfattribs={"layer": "WALL"})
    stream = StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


def test_auto_start_uses_single_available_photometry(tmp_path, monkeypatch):
    projects = ProjectStore(tmp_path / "projects")
    evidence = LocalEvidenceStore(tmp_path / "evidence.sqlite3")
    client = TestClient(create_app(project_store=projects, evidence_store=evidence,
                                   user_documents_directory=tmp_path / "documents", vision_model=False))
    project = client.post("/api/projects", json={"project_name": "Run", "space_type": "Office"}).json()
    project_id = project["project_id"]
    client.post(
        f"/api/projects/{project_id}/floor-plan",
        data={"expected_revision": "0"}, files={"file": ("room.dxf", BytesIO(_drawing()), "application/dxf")},
    )
    state = projects.get(project_id)
    plan = state.floor_plan
    executor = DialuxExecutionService(projects, executable=tmp_path / "DIALux_x64.exe")
    blocked, effective, _ = executor.readiness(
        project_id, DialuxRunRequest(expected_revision=state.revision)
    )
    assert blocked.status == "blocked"
    assert effective is None
    candidate = plan.area_candidates[0]
    model = apply_model_analysis(plan, {"rooms": [{
        "candidate_id": candidate.candidate_id, "action": "include", "name": "Office",
        "usage": "Office", "floor": "1F", "number": "1", "confidence": .99,
        "evidence": ["closed boundary"], "rationale": "bounded room",
    }], "elements": []}, model_name="test-multimodal")
    state = projects.set_floor_plan(project_id, state.revision,
                                    plan.model_copy(update={"spatial_model": model}), None)
    photometry = projects.directory / f"{project_id}.photometry" / "fixture.ies"
    photometry.parent.mkdir(parents=True, exist_ok=True)
    photometry.write_bytes(b"IESNA:LM-63-2002\n")

    captured = {}

    def fake_prepare(pid, request):
        captured["project_id"] = pid
        captured["request"] = request
        return DialuxRunRecord(run_id="automatic-run", project_id=pid, status="needs_attention",
                               profile="test", request=request, error="DIALux executable missing")

    monkeypatch.setattr(executor, "prepare", fake_prepare)
    record = executor.auto_start(project_id)
    assert captured["project_id"] == project_id
    assert captured["request"].photometry_path == str(photometry.relative_to(projects.directory)).replace("\\", "/")
    assert captured["request"].luminaire_id is None
    assert record.error == "DIALux executable missing"


def test_readiness_generates_layout_for_supported_complex_single_room(tmp_path):
    model = SpatialModel.model_validate_json(
        (Path(__file__).parent / "fixtures/phase0/bridge-spatial-model.json").read_text(encoding="utf-8")
    )
    model.automation_status = "auto_confirmed"
    model.rooms[0].boundary = [
        type(model.rooms[0].boundary[0])(x=x, y=y)
        for x, y in [(0, 0), (6, 0), (7, 2), (6, 4), (0, 4)]
    ]
    model.rooms[0].holes = [[
        type(model.rooms[0].boundary[0])(x=x, y=y)
        for x, y in [(1, 1), (2, 1), (2, 2), (1, 2)]
    ]]
    model.rooms[0].area_m2 = None
    state = ProjectState(
        project_id="complex-room",
        brief=DesignBrief(project_name="Complex room", space_type="Office"),
        floor_plan=FloorPlan(
            asset=FloorPlanAsset(source_name="room.dxf", source_type="dxf", storage_path="room.dxf",
                                 sha256=model.source_sha256, size_bytes=0),
            drawing_units="m", meters_per_drawing_unit=1, read_complete=True,
            spatial_model=model,
        ),
    )

    class Projects:
        def get(self, project_id):
            assert project_id == state.project_id
            return state

    service = DialuxExecutionService(Projects(), executable=tmp_path / "DIALux_x64.exe")
    readiness, effective, layout = service.readiness(
        state.project_id,
        DialuxRunRequest(expected_revision=state.revision, photometry_path="fixture.ies"),
    )

    assert readiness.status == "ready"
    assert readiness.can_prepare
    assert readiness.complexity == "complex"
    assert len(layout) == 1
    assert layout[0].room_id == effective.rooms[0].room_id


def test_optimization_rejects_partial_room_metrics_and_only_requires_selected_constraints():
    request = DialuxRunRequest(
        expected_revision=0,
        optimization=DialuxOptimizationRequest(
            enabled=True, max_iterations=2, target_illuminance_lx=500, min_uniformity_u0=.6,
        ),
        layout=[
            DialuxLayoutItem(room_id="room-a", position_m=[1, 1, 2], rotation_deg=[0, 0, 0]),
            DialuxLayoutItem(room_id="room-b", position_m=[1, 1, 2], rotation_deg=[0, 0, 0]),
        ],
    )
    record = DialuxRunRecord(
        run_id="run", project_id="project", status="completed", profile="test", request=request,
    )
    partial = {
        "layout_count": 1,
        "rooms": [{"metrics": {"average_illuminance_lx": {"value": 600}, "uniformity_u0": {"value": .8}}}],
    }
    complete = {
        "layout_count": 2,
        "rooms": [
            {"metrics": {"average_illuminance_lx": {"value": 500}, "uniformity_u0": {"value": .6}}},
            {"metrics": {"average_illuminance_lx": {"value": 500}, "uniformity_u0": {"value": .6}}},
        ],
    }

    selected = DialuxExecutionService._select_result(record, [partial, complete])

    assert selected["layout_count"] == 2
    assert selected["optimization"]["status"] == "feasible_candidate_selected"
    assert selected["optimization"]["secondary_objective_applied"] is False


def test_optimization_can_use_one_configured_hard_constraint():
    request = DialuxRunRequest(
        expected_revision=0,
        optimization=DialuxOptimizationRequest(enabled=True, max_iterations=2, target_illuminance_lx=400),
        layout=[DialuxLayoutItem(room_id="room-a", position_m=[1, 1, 2], rotation_deg=[0, 0, 0])],
    )
    record = DialuxRunRecord(
        run_id="run", project_id="project", status="completed", profile="test", request=request,
    )
    result = {"layout_count": 1, "rooms": [{"metrics": {"average_illuminance_lx": {"value": 400}}}]}

    selected = DialuxExecutionService._select_result(record, [result, {**result, "layout_count": 2}])

    assert selected["optimization"]["status"] == "feasible_candidate_selected"
