from __future__ import annotations

import json
import socket
from pathlib import Path
from threading import Thread

import fitz
from fastapi.testclient import TestClient
from PIL import Image

from lighting_agent.blender_workflow import (
    BlenderWorkflowError,
    create_or_reuse_blender_model,
    estimate_workplane,
    mcp_request,
    modeling_missing_fields,
    render_workflow_report_pdf,
    workflow_source_fingerprint,
)
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import (
    BlenderEstimate,
    BlenderModelAsset,
    BlenderSourceAsset,
    BlenderWorkflow,
    BlenderWorkflowParameters,
    DesignBrief,
    LuminaireCandidate,
    ProjectState,
)
from lighting_agent.web_api import create_app


def _source(digest: str = "a" * 64) -> BlenderSourceAsset:
    return BlenderSourceAsset(
        source_name="meeting-room.pdf",
        source_type="pdf",
        storage_path="workflow/sources/meeting-room.pdf",
        sha256=digest,
        size_bytes=100,
    )


def _confirmed_brief() -> DesignBrief:
    return DesignBrief(
        project_name="Meeting room",
        space_type="meeting room",
        area_m2=24,
        length_m=6,
        width_m=4,
        room_height_m=3,
        confirmed_fields={
            "space_type",
            "area_m2",
            "length_m",
            "width_m",
            "room_height_m",
        },
    )


def test_mcp_request_uses_blender_addon_json_socket_protocol(monkeypatch) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received: dict[str, object] = {}

    def serve() -> None:
        client, _ = server.accept()
        with client:
            received.update(json.loads(client.recv(8192).decode("utf-8")))
            client.sendall(json.dumps({"status": "success", "result": {"pong": True}}).encode("utf-8"))
        server.close()

    thread = Thread(target=serve)
    thread.start()
    monkeypatch.setenv("BLENDER_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("BLENDER_MCP_PORT", str(port))

    assert mcp_request("ping") == {"pong": True}
    thread.join(timeout=2)
    assert received == {"type": "ping", "params": {}}


def test_modeling_requires_source_and_confirmed_three_dimensional_geometry() -> None:
    state = ProjectState(brief=DesignBrief(project_name="Evidence gate"))
    assert modeling_missing_fields(state) == [
        "source_file",
        "length_m",
        "width_m",
        "room_height_m",
    ]

    state = ProjectState(
        brief=_confirmed_brief(),
        blender_workflow=BlenderWorkflow(source_assets=[_source()]),
    )
    assert modeling_missing_fields(state) == []


def test_matching_saved_model_is_reused_without_calling_mcp(tmp_path, monkeypatch) -> None:
    project_id = "modelreuse01"
    workflow = BlenderWorkflow(source_assets=[_source()])
    fingerprint = workflow_source_fingerprint(workflow)
    model_path = Path(f"{project_id}.blender-workflow/model/{project_id}.blend")
    render_path = Path(f"{project_id}.blender-workflow/renders/render-room.png")
    (tmp_path / model_path).parent.mkdir(parents=True)
    (tmp_path / model_path).write_bytes(b"BLENDER")
    (tmp_path / render_path).parent.mkdir(parents=True)
    Image.new("RGB", (32, 24), "white").save(tmp_path / render_path)
    model = BlenderModelAsset(
        model_path=model_path.as_posix(),
        source_sha256=fingerprint,
        model_sha256="b" * 64,
        status="ready",
        render_paths=[render_path.as_posix()],
        scene_summary={
            "selected_luminaire_ids": [],
            "modeling_parameters": {
                "workplane_height_m": 0.75,
                "grid_spacing_m": 1.0,
                "floor_reflectance": 0.2,
                "wall_reflectance": 0.5,
                "ceiling_reflectance": 0.7,
            },
        },
    )
    state = ProjectState(
        project_id=project_id,
        brief=_confirmed_brief(),
        blender_workflow=workflow.model_copy(update={"model": model}),
    )
    monkeypatch.setattr("lighting_agent.blender_workflow.probe_blender", lambda: ("connected", "ok"))
    monkeypatch.setattr(
        "lighting_agent.blender_workflow.ensure_blender_running",
        lambda **_: (_ for _ in ()).throw(AssertionError("MCP should not be called")),
    )

    reused = create_or_reuse_blender_model(
        state,
        project_root=tmp_path,
        project_id=project_id,
    )
    assert reused.reused is True
    assert reused.message == "已复用与当前资料及灯具匹配的模型。"


def test_pdf_intake_creates_conversation_preview_and_defers_model(tmp_path) -> None:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "PDF intake"}).json()
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 90), "Lighting design report - meeting room 6 m x 4 m")
    content = document.tobytes()
    document.close()

    response = client.post(
        f"/api/projects/{project['project_id']}/blender-workflow/source",
        data={"expected_revision": 0, "auto_model_enabled": "false"},
        files={"file": ("design-report.pdf", content, "application/pdf")},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["auto_model"]["status"] == "deferred"
    previews = payload["source"]["preview_paths"]
    assert len(previews) == 1
    assert payload["workflow"]["nodes"][1]["status"] == "pending"

    image = client.get(
        f"/api/projects/{project['project_id']}/blender-workflow/assets/{previews[0]}"
    )
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")


def test_report_requires_real_blender_render_and_heatmap(tmp_path) -> None:
    state = ProjectState(brief=_confirmed_brief())
    try:
        render_workflow_report_pdf(state, tmp_path, tmp_path / "report.pdf")
    except BlenderWorkflowError as error:
        assert "最终灯具" in str(error)
    else:
        raise AssertionError("report should be blocked without a completed scheme")


def test_fallback_estimate_multiplies_per_fixture_flux_by_array_count(tmp_path) -> None:
    luminaire = LuminaireCandidate(
        luminaire_id="fixture-1",
        article_name="Panel 4200 lm",
        luminous_flux_lm=4200,
        power_w=36,
        detail_url="https://example.test/fixture-1",
        matching_status="matches",
    )
    brief = _confirmed_brief().model_copy(update={"target_illuminance_lx": 500})
    parameters = BlenderWorkflowParameters(
        maintenance_factor=0.8,
        utilization_factor=0.6,
        floor_reflectance=0.2,
        wall_reflectance=0.5,
        ceiling_reflectance=0.7,
    )
    state = ProjectState(
        project_id="estimatearray01",
        brief=brief,
        luminaires=[luminaire],
        selected_luminaire_ids=[luminaire.luminaire_id],
        blender_workflow=BlenderWorkflow(source_assets=[_source()], parameters=parameters),
    )

    result = estimate_workplane(
        state,
        parameters,
        project_root=tmp_path,
        project_id=state.project_id,
        allow_provisional=False,
    )
    assert result.status == "succeeded"
    assert result.average_illuminance_lx == 504
    assert any("25200 lm" in assumption for assumption in result.assumptions)


def test_complete_optimization_report_is_a_valid_pdf(tmp_path) -> None:
    project_id = "reportmodel01"
    render = tmp_path / f"{project_id}.blender-workflow/renders/render-room.png"
    heatmap = tmp_path / f"{project_id}.blender-workflow/renders/illuminance-heatmap.png"
    render.parent.mkdir(parents=True)
    Image.new("RGB", (960, 640), (220, 225, 228)).save(render)
    Image.new("RGB", (960, 640), (44, 126, 118)).save(heatmap)
    luminaire = LuminaireCandidate(
        luminaire_id="fixture-1",
        article_name="Office Panel 36 W",
        brand_name="Example",
        power_w=36,
        luminous_flux_lm=4200,
        detail_url="https://example.test/fixture-1",
        matching_status="matches",
    )
    model = BlenderModelAsset(
        model_path=f"{project_id}.blender-workflow/model/{project_id}.blend",
        model_sha256="b" * 64,
        status="ready",
        render_paths=[render.relative_to(tmp_path).as_posix()],
        scene_summary={"selected_luminaire_ids": ["fixture-1"], "fixture_count": 6},
    )
    estimate = BlenderEstimate(
        solver_version="test",
        input_project_revision=1,
        area_m2=24,
        grid_rows=2,
        grid_columns=2,
        grid_x_coordinates_m=[1, 3],
        grid_y_coordinates_m=[1, 3],
        illuminance_lx=[[450, 500], [480, 510]],
        average_illuminance_lx=485,
        minimum_illuminance_lx=450,
        maximum_illuminance_lx=510,
        uniformity_u0=0.928,
        heatmap_path=heatmap.relative_to(tmp_path).as_posix(),
        assumptions=["维护系数 MF 为 0.8。"],
        limitations=["正式设计需使用 DIALux evo 复核。"],
    )
    state = ProjectState(
        project_id=project_id,
        brief=_confirmed_brief(),
        luminaires=[luminaire],
        selected_luminaire_ids=["fixture-1"],
        blender_workflow=BlenderWorkflow(model=model, estimate=estimate),
    )
    target = tmp_path / "optimized-lighting-scheme.pdf"

    paths = render_workflow_report_pdf(state, tmp_path, target)
    assert target.read_bytes().startswith(b"%PDF")
    assert paths == [render.relative_to(tmp_path).as_posix(), heatmap.relative_to(tmp_path).as_posix()]
