from __future__ import annotations

from io import BytesIO, StringIO

import ezdxf
from fastapi.testclient import TestClient

from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import DialuxLayoutItem, DialuxOptimizationRequest, DialuxRunRecord, DialuxRunRequest
from lighting_agent.dialux_service import DialuxExecutionService
from lighting_agent.web_api import create_app


def _drawing() -> bytes:
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = 6
    document.modelspace().add_lwpolyline([(0, 0), (6, 0), (6, 4), (0, 4)], close=True,
                                        dxfattribs={"layer": "WALL"})
    stream = StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


def test_dialux_http_workflow_stops_at_confirmation_and_records_assumptions(tmp_path):
    projects = ProjectStore(tmp_path / "projects")
    evidence = LocalEvidenceStore(tmp_path / "evidence.sqlite3")
    client = TestClient(create_app(project_store=projects, evidence_store=evidence,
                                   user_documents_directory=tmp_path / "documents"))
    project = client.post("/api/projects", json={"project_name": "Run"}).json()
    project_id = project["project_id"]
    upload = client.post(
        f"/api/projects/{project_id}/floor-plan",
        data={"expected_revision": "0"}, files={"file": ("room.dxf", BytesIO(_drawing()), "application/dxf")},
    )
    selected = client.put(
        f"/api/projects/{project_id}/floor-plan/selection",
        params={"expected_revision": upload.json()["project"]["revision"], "candidate_index": 0},
    ).json()
    photometry = client.post(
        f"/api/projects/{project_id}/dialux/light-files",
        data={"expected_revision": str(selected["revision"])},
        files={"file": ("fixture.ies", b"IESNA:LM-63-2002\n", "application/octet-stream")},
    )
    assert photometry.status_code == 201, photometry.text
    body = {
        "expected_revision": selected["revision"],
        "photometry_path": photometry.json()["path"],
        "assumptions": {"default_usage": "Office"},
    }
    readiness = client.post(f"/api/projects/{project_id}/dialux/readiness", json=body)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["readiness"]["status"] == "needs_confirmation"
    assert readiness.json()["readiness"]["complexity"] == "simple"
    power_limited = client.post(
        f"/api/projects/{project_id}/dialux/readiness",
        json={**body, "optimization": {
            "enabled": True, "max_iterations": 2, "target_illuminance_lx": 500, "max_power_w": 100,
        }},
    )
    assert power_limited.json()["readiness"]["status"] == "blocked"
    assert not power_limited.json()["readiness"]["can_prepare"]
    assert any("max_power_w" in issue for issue in power_limited.json()["readiness"]["issues"])
    unsupported_objective = client.post(
        f"/api/projects/{project_id}/dialux/readiness",
        json={**body, "optimization": {
            "enabled": True, "max_iterations": 2, "target_illuminance_lx": 500, "objective": "min_power",
        }},
    )
    assert unsupported_objective.json()["readiness"]["status"] == "blocked"
    draft = client.post(f"/api/projects/{project_id}/dialux/runs", json=body)
    assert draft.status_code == 200, draft.text
    assert draft.json()["status"] == "draft"
    assert draft.json()["readiness"]["assumptions"]["wall_thickness_m"] == .12
    confirmed = client.post(f"/api/projects/{project_id}/dialux/runs/{draft.json()['run_id']}/confirm")
    # The test machine has no DIALux executable; confirmation still records a
    # fully audited preparation failure instead of pretending to have calculated.
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] in {"needs_attention", "prepared"}


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
