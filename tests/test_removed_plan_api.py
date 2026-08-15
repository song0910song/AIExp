from __future__ import annotations

from io import BytesIO, StringIO

import ezdxf
from fastapi.testclient import TestClient

from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.web_api import create_app


def _dxf_with_closed_room() -> bytes:
    document = ezdxf.new("R2010")
    document.units = ezdxf.units.M
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (6, 0), (6, 5), (0, 5)],
        close=True,
        dxfattribs={"layer": "ROOM"},
    )
    modelspace.add_text("会议室", dxfattribs={"height": 0.3}).set_placement((1, 1))
    output = StringIO()
    document.write(output)
    return output.getvalue().encode("utf-8")


def _dxf_with_segment_walls() -> bytes:
    """Closed room drawn as disconnected wall segments (no closed polyline)."""
    document = ezdxf.new("R2010")
    document.units = ezdxf.units.M
    modelspace = document.modelspace()
    # 4 walls drawn as separate LINE segments on the DIALux-style contour layer.
    segments = [
        ((0, 0), (6, 0)),
        ((6, 0), (6, 5)),
        ((6, 5), (0, 5)),
        ((0, 5), (0, 0)),
    ]
    for start, end in segments:
        modelspace.add_line(start, end, dxfattribs={"layer": "DLX_CONT"})
    output = StringIO()
    document.write(output)
    return output.getvalue().encode("utf-8")


def test_floor_plan_reconstructs_area_from_wall_segments(tmp_path) -> None:
    """Closed room expressed as a segment network is reconstructed via shapely."""
    from lighting_agent.floor_plan import parse_floor_plan
    from pathlib import Path

    target = tmp_path / "segments.dxf"
    target.write_bytes(_dxf_with_segment_walls())
    plan = parse_floor_plan(target, storage_path="segments.dxf")

    assert plan.area_candidates, "墙线网络应重构出房间轮廓候选"
    assert plan.area_candidates[0].area_m2 == 30
    assert plan.area_candidates[0].length_m == 6
    assert plan.area_candidates[0].width_m == 5
    assert plan.area_candidates[0].layer == "DLX_CONT"


def test_floor_plan_upload_and_apply_updates_project_geometry(tmp_path) -> None:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "CAD plan"}).json()

    uploaded = client.post(
        f"/api/projects/{project['project_id']}/floor-plan",
        data={"expected_revision": "0"},
        files={"file": ("meeting-room.dxf", BytesIO(_dxf_with_closed_room()), "application/dxf")},
    )

    assert uploaded.status_code == 201
    floor_plan = uploaded.json()["floor_plan"]
    assert floor_plan["drawing_units"] == "m"
    assert floor_plan["room_name"] == "会议室"
    assert floor_plan["area_candidates"][0]["area_m2"] == 30

    state = uploaded.json()["project"]
    assert state["brief"]["area_m2"] == 30
    assert state["brief"]["length_m"] == 6
    assert state["brief"]["width_m"] == 5
    assert state["floor_plan"]["asset"]["source_name"] == "meeting-room.dxf"


def test_floor_plan_rejects_unsupported_file_type(tmp_path) -> None:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "Invalid plan"}).json()

    response = client.post(
        f"/api/projects/{project['project_id']}/floor-plan",
        data={"expected_revision": "0"},
        files={"file": ("meeting-room.pdf", BytesIO(b"not a drawing"), "application/pdf")},
    )

    assert response.status_code == 415


def test_room_name_strips_measurement_suffix() -> None:
    """Room name coupled with power density like '512会议室 (9.01 W/m²)' is still found."""
    from lighting_agent.floor_plan import _room_name

    assert _room_name(["512会议室 (9.01 W/m²)"]) == "会议室"
    assert _room_name(["机房 30 m²"]) == "机房"
    # 纯测量标注没有房间名，不应误报。
    assert _room_name(["9.01 W/m²"]) is None

