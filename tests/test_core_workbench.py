from __future__ import annotations

import json
from io import BytesIO, StringIO
from types import SimpleNamespace

import ezdxf
import pytest
from fastapi.testclient import TestClient

from lighting_agent.config import Settings
from lighting_agent.main import build_parser
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import DesignBrief, LuminaireCandidate, LuminaireSearchRequest, LuminaireSearchRun
from lighting_agent.tools import make_tools
from lighting_agent.web_api import create_app


class Catalogue:
    def search_with_run(self, request, *, project_id=None, project_revision=None):
        fixture = LuminaireCandidate(
            luminaire_id="demo-1", article_name="Demo 4000K", brand_name="Demo",
            detail_url="https://luminaires.dialux.com/zh/article/demo-1",
            power_w=18, cct_k=4000, matching_status="matches",
        )
        run = LuminaireSearchRun(
            request=request, original_keyword=request.keyword, resolved_keyword=request.keyword,
            endpoint="https://luminaires.dialux.com/test", candidate_ids=[fixture.luminaire_id],
        )
        return SimpleNamespace(candidates=[fixture], search_run=run)

    def search(self, request: LuminaireSearchRequest):
        return self.search_with_run(request).candidates

    def resolve_send_to_dialux_url(self, detail_url):
        assert detail_url.endswith("demo-1")
        return "dial://example.invalid/demo.uld"


@pytest.fixture
def environment(tmp_path):
    projects = ProjectStore(tmp_path / "projects")
    evidence = LocalEvidenceStore(tmp_path / "evidence.sqlite3")
    app = create_app(
        project_store=projects, evidence_store=evidence, dialux_api=Catalogue(),
        user_documents_directory=tmp_path / "documents",
    )
    return TestClient(app), projects, evidence


def drawing() -> bytes:
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = 6
    document.modelspace().add_lwpolyline(
        [(0, 0), (5, 0), (5, 4), (0, 4)],
        close=True,
        dxfattribs={"layer": "WALL"},
    )
    buffer = StringIO()
    document.write(buffer)
    return buffer.getvalue().encode("utf-8")


def test_route_inventory_only_exposes_four_capabilities(environment):
    client, _, _ = environment
    paths = {route.path for route in client.app.routes}
    assert "/api/projects/{project_id}/floor-plan" in paths
    assert "/api/evidence/search" in paths
    assert "/api/chat" in paths
    assert "/api/projects/{project_id}/luminaires/{luminaire_id}/send-to-dialux" in paths
    assert not any(
        fragment in path
        for path in paths
        for fragment in ("calculations", "rule-checks", "redesign", "dialux-results", "photometry")
    )
    assert set(build_parser()._subparsers._group_actions[0].choices) == {
        "init-project", "show-project", "analyze-cad", "add-document",
        "search-evidence", "search-luminaires",
    }


def test_cad_upload_keeps_geometry_unconfirmed_until_user_selects_it(environment):
    client, projects, _ = environment
    project = client.post("/api/projects", json={"project_name": "Room"}).json()
    project_id = project["project_id"]
    response = client.post(
        f"/api/projects/{project_id}/floor-plan",
        data={"expected_revision": "0"},
        files={"file": ("room.dxf", BytesIO(drawing()), "application/dxf")},
    )
    assert response.status_code == 201, response.text
    uploaded = response.json()["project"]
    assert uploaded["floor_plan"]["area_candidates"][0]["area_m2"] == 20
    assert uploaded["floor_plan"]["selected_area_candidate_index"] is None
    assert projects.get(project_id).brief.area_m2 is None

    confirmed = client.put(
        f"/api/projects/{project_id}/floor-plan/selection",
        params={"expected_revision": uploaded["revision"], "candidate_index": 0},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["floor_plan"]["selected_area_candidate_index"] == 0
    assert projects.get(project_id).brief.area_m2 == 20
    assert "area_m2" in projects.get(project_id).brief.confirmed_fields


def test_project_documents_are_private_but_global_documents_are_searchable(environment):
    client, _, _ = environment
    one = client.post("/api/projects", json={"project_name": "One"}).json()["project_id"]
    two = client.post("/api/projects", json={"project_name": "Two"}).json()["project_id"]
    global_doc = client.post(
        "/api/documents", files={"file": ("standard.md", b"Global standard 500 lx", "text/markdown")}
    )
    local_doc = client.post(
        f"/api/projects/{one}/documents",
        files={"file": ("private.md", b"Private project fixture detail", "text/markdown")},
    )
    assert global_doc.status_code == local_doc.status_code == 201
    private_hash = client.get(f"/api/projects/{one}/documents").json()[0]["source_hash"]
    assert client.get(f"/api/projects/{two}/documents/{private_hash}").status_code == 404
    global_results = client.post("/api/evidence/search", json={"query": "Private fixture"}).json()["evidence"]
    assert global_results == []
    private_results = client.post(
        "/api/evidence/search", json={"query": "Private fixture", "project_id": one}
    ).json()["evidence"]
    assert private_results[0]["source_name"] == "private.md"
    other_results = client.post(
        "/api/evidence/search", json={"query": "Global standard", "project_id": two}
    ).json()["evidence"]
    assert other_results[0]["source_name"] == "standard.md"
    assert client.delete(f"/api/projects/{one}").status_code == 204
    assert client.get(f"/api/projects/{one}").status_code == 404
    assert client.post("/api/evidence/search", json={"query": "Global standard"}).json()["evidence"]


def test_keyword_search_saves_fixture_and_explicit_send_launches_handler(environment, monkeypatch):
    client, _, _ = environment
    project = client.post("/api/projects", json={"project_name": "Product"}).json()
    project_id = project["project_id"]
    response = client.post(
        f"/api/projects/{project_id}/luminaires",
        json={"keyword": "downlight", "expected_revision": project["revision"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["project"]["luminaires"][0]["luminaire_id"] == "demo-1"
    launched = []
    monkeypatch.setattr("lighting_agent.web_api.open_in_dialux", launched.append)
    result = client.post(f"/api/projects/{project_id}/luminaires/demo-1/send-to-dialux")
    assert result.status_code == 200
    assert result.json()["status"] == "launched"
    assert launched == ["dial://example.invalid/demo.uld"]
    assert client.post(f"/api/projects/{project_id}/luminaires/unknown/send-to-dialux").status_code == 404


def test_agent_tools_are_read_only(environment):
    _, projects, evidence = environment
    tools = make_tools(projects=projects, evidence=evidence, dialux=Catalogue(), project_id=None)
    assert {item.name for item in tools} == {"get_project", "search_evidence", "search_luminaires"}
    assert tools[2].invoke({"keyword": "panel"})["candidates"][0]["luminaire_id"] == "demo-1"


def test_updating_old_project_preserves_historical_fields(environment):
    client, projects, _ = environment
    project = client.post("/api/projects", json={"project_name": "Legacy"}).json()
    with projects.database.transaction() as connection:
        stored = projects.get(project["project_id"]).model_dump(mode="json")
        stored["simulation_runs"] = [{"old_result": 510}]
        stored["calculations"] = [{"old_estimate": 520}]
        connection.execute(
            "UPDATE projects SET state_json = ? WHERE project_id = ?",
            (json.dumps(stored), project["project_id"]),
        )
    renamed = client.put(
        f"/api/projects/{project['project_id']}/brief",
        json={"expected_revision": 0, "project_name": "Legacy renamed", "space_type": "meeting room"},
    )
    assert renamed.status_code == 200, renamed.text
    saved = projects.get(project["project_id"]).model_dump(mode="json")
    assert saved["simulation_runs"] == [{"old_result": 510}]
    assert saved["calculations"] == [{"old_estimate": 520}]
    assert "simulation_runs" not in renamed.json()


def test_chat_rejects_another_projects_session(environment, monkeypatch):
    _, projects, evidence = environment
    from lighting_agent import web_api

    monkeypatch.setattr(
        web_api, "Settings",
        lambda: SimpleNamespace(
            llm_api_key="test", llm_model="test", rag_backend="local",
            chat_session_max_messages=80, chat_session_ttl_hours=168, agent_max_steps=12,
            supported_reasoning_efforts=lambda: ("low", "medium", "high"),
            default_reasoning_effort=lambda: "medium",
            prompt_cache_options=lambda: None,
            with_reasoning_effort=lambda effort: SimpleNamespace(llm_reasoning_effort=effort),
        ),
    )
    client = TestClient(web_api.create_app(
        project_store=projects, evidence_store=evidence, dialux_api=Catalogue(),
    ))
    project_one = client.post("/api/projects", json={"project_name": "First"}).json()["project_id"]
    project_two = client.post("/api/projects", json={"project_name": "Second"}).json()["project_id"]
    used_efforts = []
    def fake_agent(config, **kwargs):
        used_efforts.append(config.llm_reasoning_effort)
        return SimpleNamespace(
            invoke=lambda *args, **kwargs: {"messages": [SimpleNamespace(content="The answer")]}
        )
    monkeypatch.setattr(web_api, "build_agent", fake_agent)
    response = client.post(
        "/api/chat", json={"project_id": project_one, "message": "Hi", "reasoning_effort": "high"}
    )
    assert response.status_code == 200, response.text
    assert used_efforts == ["high"]
    assert client.post(
        "/api/chat", json={"project_id": project_one, "message": "Hi", "reasoning_effort": "none"}
    ).status_code == 422
    session_id = response.json()["session_id"]
    assert client.get(f"/api/chat/{session_id}", params={"project_id": project_two}).status_code == 404
    assert client.get(f"/api/chat/{session_id}", params={"project_id": project_one}).json()["messages"]


def test_project_chat_search_saves_candidates_for_explicit_handoff(environment):
    _, projects, evidence = environment
    state = projects.create(DesignBrief(project_name="Search"))
    tools = make_tools(
        projects=projects, evidence=evidence, dialux=Catalogue(), project_id=state.project_id
    )
    search = next(item for item in tools if item.name == "search_luminaires")
    result = search.invoke({"keyword": "downlight"})
    assert result["candidates"][0]["luminaire_id"] == "demo-1"
    assert projects.get(state.project_id).luminaires[0].luminaire_id == "demo-1"


def test_reasoning_and_prompt_cache_settings_remain_available():
    config = Settings(
        llm_model="gpt-5.6-test", llm_base_url="https://api.openai.com/v1",
        llm_prompt_cache_enabled=None, llm_reasoning_efforts="low,medium,high",
    )
    assert config.prompt_cache_options() == {"mode": "implicit", "ttl": "30m"}
    assert config.with_reasoning_effort("high").llm_reasoning_effort == "high"
    assert config.default_reasoning_effort() == "medium"
    assert Settings(
        llm_model="gpt-5.6-test", llm_base_url="https://gateway.example/v1",
        llm_prompt_cache_enabled=None,
    ).prompt_cache_options() is None
    assert Settings(llm_prompt_cache_enabled=True).prompt_cache_options() == {"mode": "implicit", "ttl": "30m"}
