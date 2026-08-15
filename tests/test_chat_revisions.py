from __future__ import annotations

import json

from fastapi.testclient import TestClient

import lighting_agent.web_api as web_api
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.web_api import create_app
from tests.test_web_api import FakeDialux, FakeStreamingAgent


def test_streamed_chat_returns_latest_project_and_drops_old_revision_context(tmp_path, monkeypatch) -> None:
    agent = FakeStreamingAgent()
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: agent)
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=FakeDialux(),
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "Chat revision"}).json()

    first = client.post(
        "/api/chat/stream",
        json={"message": "first question", "project_id": project["project_id"]},
    )
    first_events = [json.loads(line) for line in first.text.splitlines()]
    session_id = first_events[0]["session_id"]
    assert first_events[-1]["project"]["revision"] == 0
    assert "authoritative current revision is 0" in agent.requests[0]["messages"][-1]["content"]

    updated = client.put(
        f"/api/projects/{project['project_id']}/brief",
        json={"expected_revision": 0, "brief": project["brief"]},
    ).json()
    assert updated["revision"] == 1

    second = client.post(
        "/api/chat/stream",
        json={"message": "second question", "session_id": session_id, "project_id": project["project_id"]},
    )
    assert second.status_code == 200
    request_messages = agent.requests[1]["messages"]
    assert request_messages[0]["content"] == "first question"
    assert "authoritative current revision is 1" in request_messages[-1]["content"]
    assert "revision is 0" not in request_messages[-1]["content"]


def test_agent_luminaire_search_rebases_an_obsolete_chat_revision(tmp_path, monkeypatch) -> None:
    import lighting_agent.tools as agent_tools
    from lighting_agent.schemas import DesignBrief, ProjectUpdate

    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="Agent luminaire search",
            space_type="会议室",
            mounting="recessed",
            target_cct_k=4000,
        )
    )
    store.update(project.project_id, ProjectUpdate(expected_revision=project.revision))
    monkeypatch.setattr(agent_tools, "project_store", store)
    monkeypatch.setattr(agent_tools, "DialuxAPI", lambda: FakeDialux())

    result = agent_tools.search_luminaires.invoke(
        {
            "keyword": "panel",
            "project_id": project.project_id,
            "expected_revision": 0,
        }
    )

    assert result["rebased"] is True
    assert result["saved_count"] == 1
    assert result["project_revision"] == 2


def test_agent_append_tools_rebase_stale_revision_within_one_chat_turn(tmp_path, monkeypatch) -> None:
    import lighting_agent.tools as agent_tools
    from lighting_agent.schemas import CalculationInput, DesignBrief

    store = ProjectStore(tmp_path / "projects")
    project = store.create(DesignBrief(project_name="Agent revision rebase"))
    monkeypatch.setattr(agent_tools, "project_store", store)

    inputs = CalculationInput(
        area_m2=30,
        target_illuminance_lx=500,
        luminaire_luminous_flux_lm=3200,
        luminaire_power_w=24,
        utilization_factor=0.6,
        maintenance_factor=0.8,
    )
    first = agent_tools.calculate_preliminary_lighting.invoke(
        {"project_id": project.project_id, "expected_revision": 0, "inputs": inputs}
    )
    second = agent_tools.calculate_preliminary_lighting.invoke(
        {"project_id": project.project_id, "expected_revision": 0, "inputs": inputs}
    )

    assert first["project_revision"] == 1
    assert second["project_revision"] == 2
    assert second["rebased"] is True
    assert len(store.get(project.project_id).calculations) == 2
