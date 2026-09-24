"""File-driven branching, text-only upload requests and removed deliverables."""

import json
from pathlib import Path

import fitz
import pytest
from langchain_core.messages import ToolMessage

import lighting_agent.tools as agent_tools
import lighting_agent.web_api as web_api
from lighting_agent import agent, main
from lighting_agent.project_store import ProjectStore
from lighting_agent.redesign_service import _resolve_report
from lighting_agent.schemas import DesignBrief, LuminaireCandidate, ProjectState
from lighting_agent.workflow import project_workflow
from tests.test_removed_plan_api import _dxf_with_closed_room
from tests.test_web_api import FakeStreamingAgent, make_client


def _pdf(text="DIALux evo existing lighting report"):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), text)
        return document.tobytes()


def _upload_report(client, project_id, text="DIALux evo existing lighting report", name="report.pdf"):
    response = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": (name, _pdf(text), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_plan(client, state):
    response = client.post(
        f"/api/projects/{state['project_id']}/floor-plan",
        data={"expected_revision": state["revision"]},
        files={"file": ("room.dxf", _dxf_with_closed_room(), "application/dxf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def test_text_only_project_requests_files_after_selection(tmp_path):
    store = ProjectStore(tmp_path)
    state = store.create(DesignBrief(project_name="文字新建"))
    assert project_workflow(tmp_path, state)["stage"] == "standard_design"
    state, _, _ = store.append_luminaires(state.project_id, state.revision, [
        LuminaireCandidate(luminaire_id="fixture", article_name="Panel", detail_url="https://example.test/panel", matching_status="matches")
    ])
    result = project_workflow(tmp_path, state)
    assert result["stage"] == "awaiting_redesign_files"
    assert result["upload_request_format"] == "plain_text"
    assert "DXF" in result["message"] and "PDF" in result["message"]
    assert "fields" not in result
    assert not state.selected_luminaire_ids  # No final-selection gate.


@pytest.mark.parametrize("report_first", [True, False])
def test_both_upload_orders_route_directly_to_redesign(tmp_path, report_first):
    client = make_client(tmp_path)
    state = client.post("/api/projects", json={"project_name": "带资料新建"}).json()
    root = tmp_path / "projects"
    if report_first:
        _upload_report(client, state["project_id"])
        pending = project_workflow(root, ProjectState.model_validate(state))
        assert pending["missing_files"] == ["DIALux 导出的 DXF 平面图"]
        state = _upload_plan(client, state)
    else:
        state = _upload_plan(client, state)
        pending = project_workflow(root, ProjectState.model_validate(state))
        assert pending["missing_files"] == ["DIALux PDF 设计报告"]
        _upload_report(client, state["project_id"])
    state = ProjectStore(root).get(state["project_id"])
    result = project_workflow(root, state)
    assert result["stage"] == "analyze_existing_design"
    assert not result["missing_files"]
    assert not state.luminaires and not state.calculations
    assert _resolve_report(root, state, None) == root / result["report_source"]
    analyzed = agent_tools.analyze_dialux_report.invoke({"project_id": state.project_id})
    assert analyzed["status"] == "ok"
    assert analyzed["cross_validation"] is not None
    # A simple CAD boundary is not a DIALux export with a fixture/evaluation grid.
    assert analyzed["cross_validation"]["warnings"]
    assert not list(root.glob("*.dialux-task.zip"))


def test_standard_pdf_does_not_count_as_dialux_report(tmp_path):
    client = make_client(tmp_path)
    state = client.post("/api/projects", json={"project_name": "普通资料"}).json()
    state = _upload_plan(client, state)
    _upload_report(client, state["project_id"], "Lighting standard, not a simulation report")
    workflow = project_workflow(tmp_path / "projects", ProjectState.model_validate(state))
    assert workflow["report_sources"] == []
    assert workflow["missing_files"] == ["DIALux PDF 设计报告"]


def test_reports_are_project_scoped_and_ambiguous_reports_require_choice(tmp_path):
    client = make_client(tmp_path)
    first = client.post("/api/projects", json={"project_name": "甲"}).json()
    second = client.post("/api/projects", json={"project_name": "乙"}).json()
    _upload_report(client, first["project_id"])
    assert project_workflow(tmp_path / "projects", ProjectState.model_validate(second))["report_sources"] == []
    first = _upload_plan(client, first)
    _upload_report(client, first["project_id"], "DIALux alternative report", "alternative.pdf")
    workflow = project_workflow(tmp_path / "projects", ProjectState.model_validate(first))
    assert workflow["stage"] == "choose_report"
    assert workflow["report_source"] is None
    result = agent_tools.analyze_dialux_report.invoke({"project_id": first["project_id"]})
    assert result["status"] == "needs_input"


def test_uploaded_files_survive_new_chat_and_are_injected_into_context(tmp_path, monkeypatch):
    fake = FakeStreamingAgent()
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: fake)
    client = make_client(tmp_path)
    state = client.post("/api/projects", json={"project_name": "恢复对话"}).json()
    state = _upload_plan(client, state)
    _upload_report(client, state["project_id"])
    response = client.post("/api/chat/stream", json={"project_id": state["project_id"], "message": "继续"})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "done"
    content = fake.requests[-1]["messages"][-1]["content"]
    assert "analyze_existing_design" in content
    assert "report.pdf" in content
    assert not any(event["type"] == "clarification" for event in events)


def test_upload_question_never_emits_a_form():
    result = agent_tools.ask_user.invoke({
        "title": "补充文件", "question": "请上传平面图和 PDF 报告。",
        "fields": [{"field_id": "upload_files", "label": "上传文件"}],
    })
    assert result["status"] == "awaiting_file_upload"
    assert "fields" not in result
    message = ToolMessage(content=json.dumps(result), name="ask_user", tool_call_id="upload")
    assert web_api._clarification_from_tool_chunk(message) is None
    assert not web_api._claims_structured_clarification("请上传 PDF 报告，不需要填写表单。")


def test_task_and_report_features_are_removed_everywhere(tmp_path):
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "已移除"}).json()
    for kind in ("dialux-task", "report"):
        path = f"/api/projects/{project['project_id']}/deliverables/{kind}"
        assert client.post(path, params={"expected_revision": 0}).status_code == 404
        assert client.get(path).status_code == 404
    assert not hasattr(agent_tools, "create_dialux_task_package")
    assert not hasattr(agent_tools, "generate_design_report")
    assert not hasattr(agent, "create_dialux_task_package")
    assert not hasattr(agent, "generate_design_report")
    help_text = main.build_parser().format_help()
    assert "create-dialux-task" not in help_text
    assert "generate-report" not in help_text


def test_structured_result_can_be_imported_without_task_or_selection(tmp_path):
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "直接上传"}).json()
    response = client.post(f"/api/projects/{project['project_id']}/dialux-results", json={
        "expected_revision": 0, "metrics": {"maintained_illuminance_lx": 500},
    })
    assert response.status_code == 201
    assert response.json()["simulation_run"]["verification_status"] == "unverified"


def test_prompt_prioritizes_file_branch_and_plain_text_request():
    assert "优先于新建项目流程" in agent.SYSTEM_PROMPT
    assert "索要文件不得调用 ask_user" in agent.SYSTEM_PROMPT
    assert "完成候选推荐后也应提醒补充文件" in agent.SYSTEM_PROMPT


def test_invalid_cad_requests_usable_export_in_text(tmp_path):
    client = make_client(tmp_path)
    state = client.post("/api/projects", json={"project_name": "普通 CAD"}).json()
    state = _upload_plan(client, state)
    result = agent_tools.analyze_dxf_design.invoke({"project_id": state["project_id"]})
    assert result["status"] == "needs_valid_dxf"
    assert "DIALux DXF" in result["message"]
    assert "fields" not in result


def test_dwg_and_unreadable_report_do_not_start_redesign(tmp_path):
    from lighting_agent.schemas import FloorPlan, FloorPlanAsset

    state = ProjectState(brief=DesignBrief(project_name="无效资料"))
    directory = tmp_path / f"{state.project_id}.documents"
    directory.mkdir()
    (directory / "broken.pdf").write_bytes(b"%PDF-broken")
    state.floor_plan = FloorPlan(
        asset=FloorPlanAsset(source_name="room.dwg", source_type="dwg", storage_path="room.dwg", sha256="a" * 64, size_bytes=0),
        drawing_units="m",
    )
    result = project_workflow(tmp_path, state)
    assert result["stage"] == "standard_design"
    assert result["report_source"] is None
    assert "另存为 DXF" in result["message"]


def test_text_first_then_file_upload_enters_redesign_without_reselection(tmp_path):
    client = make_client(tmp_path)
    state = client.post("/api/projects", json={"project_name": "文字后补传"}).json()
    store = ProjectStore(tmp_path / "projects")
    selected, _, _ = store.append_luminaires(state["project_id"], 0, [
        LuminaireCandidate(luminaire_id="panel", article_name="Panel", detail_url="https://example.test/panel", matching_status="matches")
    ])
    assert project_workflow(store.directory, selected)["stage"] == "awaiting_redesign_files"
    state = _upload_plan(client, selected.model_dump(mode="json"))
    _upload_report(client, state["project_id"])
    result = agent_tools.get_project.invoke({"project_id": state["project_id"]})
    assert result["workflow"]["stage"] == "analyze_existing_design"
    assert result["selected_luminaire_ids"] == []
    assert len(result["luminaires"]) == 1


def test_real_dialux_dxf_can_be_analyzed_with_autodiscovered_report(tmp_path):
    client = make_client(tmp_path)
    state = client.post("/api/projects", json={"project_name": "DIALux 导出图纸"}).json()
    fixture = Path(__file__).parent / "fixtures" / "phase0" / "sample-room.dxf"
    response = client.post(
        f"/api/projects/{state['project_id']}/floor-plan",
        data={"expected_revision": 0},
        files={"file": ("dialux-room.dxf", fixture.read_bytes(), "application/dxf")},
    )
    assert response.status_code == 201
    _upload_report(client, state["project_id"])
    drawing = agent_tools.analyze_dxf_design.invoke({"project_id": state["project_id"]})
    report = agent_tools.analyze_dialux_report.invoke({"project_id": state["project_id"]})
    assert drawing["status"] == "ok"
    assert "eval_grid" in drawing["snapshot"]
    assert report["status"] == "ok"
    assert report["cross_validation"]["checks"]


def test_duplicate_report_content_is_not_ambiguous(tmp_path):
    state = ProjectState(brief=DesignBrief(project_name="重复报告"))
    directory = tmp_path / f"{state.project_id}.documents"
    directory.mkdir()
    content = _pdf()
    (directory / "report.pdf").write_bytes(content)
    (directory / "report-copy.pdf").write_bytes(content)
    result = project_workflow(tmp_path, state)
    assert len(result["report_sources"]) == 1


def test_clarification_fallback_does_not_create_a_form_for_uploads():
    assert not web_api._claims_structured_clarification("请上传 DXF 和 PDF。已生成问询，请填写后继续。")
