from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

import lighting_agent.web_api as web_api
from lighting_agent.config import Settings
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import LuminaireCandidate
from lighting_agent.web_api import create_app


class FakeDialux:
    def search(self, _request: Any) -> list[LuminaireCandidate]:
        return [
            LuminaireCandidate(
                luminaire_id="fixture-1",
                article_name="DL-01",
                brand_name="Example",
                power_w=20,
                cct_k=4000,
                cri=90,
                ip_rating="IP20",
                detail_url="https://example.test/fixture-1",
                matching_status="matches",
            )
        ]


class FakeStreamingAgent:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def stream(self, request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        self.requests.append(request)
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessageChunk(content="## 设计建议\n") , {"langgraph_node": "model"}
        yield AIMessageChunk(content="- 先确认照度目标") , {"langgraph_node": "model"}


class FakeTraceAgent:
    def stream(self, _request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessage(
            content="",
            tool_calls=[{"name": "get_project", "args": {"project_id": "demo"}, "id": "project-call"}],
        ), {"langgraph_node": "model"}
        yield ToolMessage(content='{"project_id":"demo"}', name="get_project", tool_call_id="project-call"), {"langgraph_node": "tools"}
        yield AIMessageChunk(content="已读取项目。"), {"langgraph_node": "model"}


class FakePartialToolCallAgent:
    """Simulate the blank tool-name chunks sent by some streaming providers."""

    def stream(self, _request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": "", "args": "", "id": "call-partial", "index": 0}],
        ), {"langgraph_node": "model"}
        yield AIMessage(
            content="",
            tool_calls=[{"name": "get_project", "args": {"project_id": "demo"}, "id": "project-call"}],
        ), {"langgraph_node": "model"}
        yield ToolMessage(content='{"project_id":"demo"}', name="get_project", tool_call_id="project-call"), {"langgraph_node": "tools"}
        yield AIMessageChunk(content="已读取项目。"), {"langgraph_node": "model"}


class FakeUnpairedToolCallAgent:
    """Simulate a provider ending a stream before ToolMessage is surfaced."""

    def stream(self, _request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessage(
            content="",
            tool_calls=[{"name": "get_project", "args": {"project_id": "demo"}, "id": "unpaired-call"}],
        ), {"langgraph_node": "model"}
        yield AIMessageChunk(content="模型已结束回答。"), {"langgraph_node": "model"}


class FakeUsageStreamingAgent:
    def stream(self, _request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessageChunk(
            content="上下文用量已返回。",
            usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
        ), {"langgraph_node": "model"}


class FakeClarificationAgent:
    def stream(self, _request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessage(
            content="",
            tool_calls=[{"name": "ask_user", "args": {}, "id": "clarification-call"}],
        ), {"langgraph_node": "model"}
        yield ToolMessage(
            content=json.dumps(
                {
                    "status": "awaiting_user_input",
                    "title": "补充设计条件",
                    "question": "请确认以下输入后继续初算。",
                    "fields": [
                        {
                            "field_id": "target_illuminance_lx",
                            "label": "目标照度",
                            "description": "工作面维持照度。",
                            "input_type": "select",
                            "required": True,
                            "placeholder": None,
                            "options": [
                                {"label": "300 lx", "value": "300"},
                                {"label": "500 lx", "value": "500"},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            name="ask_user",
            tool_call_id="clarification-call",
        ), {"langgraph_node": "tools"}
        yield AIMessageChunk(content="请填写上方条件，我会继续执行。"), {"langgraph_node": "model"}


class FakeClaimedClarificationAgent:
    """Simulate a provider that claims a form exists but never calls ask_user."""

    def stream(self, _request: dict[str, Any], *, stream_mode: str, config: dict[str, Any]):
        assert stream_mode == "messages"
        assert config["recursion_limit"] == max(4, Settings().agent_max_steps)
        yield AIMessage(
            content="",
            tool_calls=[{"name": "get_project", "args": {"project_id": "demo"}, "id": "project-call"}],
        ), {"langgraph_node": "model"}
        yield ToolMessage(content='{"project_id":"demo"}', name="get_project", tool_call_id="project-call"), {"langgraph_node": "tools"}
        yield AIMessageChunk(content="已生成结构化询问，请填写后继续。"), {"langgraph_node": "model"}


def make_client(tmp_path) -> TestClient:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=FakeDialux(),
    )
    return TestClient(app)


def test_web_project_calculation_and_luminaire_flow(tmp_path) -> None:
    client = make_client(tmp_path)
    created = client.post(
        "/api/projects",
        json={
            "project_name": "Web 会议室",
            "space_type": "会议室",
            "area_m2": 30,
            "mounting_height_m": 2.7,
            "target_illuminance_lx": 500,
            "mounting": "recessed",
            "target_cct_k": 4000,
        },
    )
    assert created.status_code == 201
    project = created.json()

    calculated = client.post(
        f"/api/projects/{project['project_id']}/calculations",
        json={
            "expected_revision": 0,
            "inputs": {
                "area_m2": 30,
                "target_illuminance_lx": 500,
                "luminaire_luminous_flux_lm": 3200,
                "luminaire_power_w": 24,
                "utilization_factor": 0.6,
                "maintenance_factor": 0.8,
            },
        },
    )
    assert calculated.status_code == 200
    assert calculated.json()["calculation"]["luminaire_count"] == 10

    luminaires = client.post(
        f"/api/projects/{project['project_id']}/luminaires",
        json={
            "keyword": "downlight",
            "mounting": "recessed",
            "target_cct_k": 4000,
            "expected_revision": 1,
            "max_results": 1,
        },
    )
    assert luminaires.status_code == 200
    assert luminaires.json()["project"]["revision"] == 2
    assert luminaires.json()["candidates"][0]["cri"] == 90


def test_web_revision_conflict_and_health(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "冲突测试"}).json()
    conflict = client.put(
        f"/api/projects/{project['project_id']}/brief",
        json={"expected_revision": 99, "brief": project["brief"]},
    )
    assert conflict.status_code == 409
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["project_count"] == 1


def test_web_deletes_project(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "删除测试"}).json()

    deleted = client.delete(f"/api/projects/{project['project_id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/projects/{project['project_id']}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_web_luminaire_save_rebases_stale_revision_and_deduplicates(tmp_path) -> None:
    client = make_client(tmp_path)
    project = client.post(
        "/api/projects",
        json={
            "project_name": "灯具并发保存",
            "space_type": "办公室",
            "mounting": "recessed",
            "target_cct_k": 4000,
        },
    ).json()
    request = {
        "keyword": "downlight",
        "mounting": "recessed",
        "target_cct_k": 4000,
        "expected_revision": 0,
        "max_results": 1,
    }

    first = client.post(f"/api/projects/{project['project_id']}/luminaires", json=request)
    assert first.status_code == 200
    assert first.json()["project"]["revision"] == 1
    assert first.json()["saved_count"] == 1
    assert first.json()["rebased"] is False

    # A second tab can still carry revision 0.  Candidate saving only appends,
    # therefore it can safely merge with the latest state instead of returning
    # a 409.  The same DIALux candidate is not saved twice.
    stale = client.post(f"/api/projects/{project['project_id']}/luminaires", json=request)
    assert stale.status_code == 200
    assert stale.json()["project"]["revision"] == 1
    assert stale.json()["saved_count"] == 0
    assert stale.json()["rebased"] is True
    assert len(stale.json()["project"]["luminaires"]) == 1


def test_web_chat_stream_returns_ndjson_and_keeps_session(tmp_path, monkeypatch) -> None:
    agent = FakeStreamingAgent()
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: agent)
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "请给出建议"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["start", "delta", "delta", "done"]
    assert events[-1]["answer"] == "## 设计建议\n- 先确认照度目标"

    client.post("/api/chat/stream", json={"message": "继续", "session_id": events[0]["session_id"]})
    assert len(agent.requests[1]["messages"]) == 3


def test_web_agent_stream_exposes_plan_and_real_tool_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeTraceAgent())
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "开始执行", "mode": "agent"})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "start"
    assert events[1]["type"] == "plan"
    assert events[1]["steps"][0]["id"] == "project"
    assert {"type": "tool_start", "name": "get_project", "call_id": "project-call"} in events
    assert {"type": "tool_end", "name": "get_project", "call_id": "project-call", "status": "done"} in events
    assert any(event == {"type": "step", "step_id": "project", "status": "done"} for event in events)
    assert events[-1]["type"] == "done"


def test_web_agent_stream_includes_safe_debug_tool_summaries_when_requested(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeTraceAgent())
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "开始执行", "debug": True})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    tool_start = next(event for event in events if event["type"] == "tool_start")
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_start["input"] == {"project_id": "demo"}
    assert isinstance(tool_start["started_at"], str)
    assert tool_end["output"] == {"project_id": "demo"}
    assert isinstance(tool_end["duration_ms"], int)


def test_debug_values_redact_sensitive_tool_arguments() -> None:
    assert web_api._debug_value(
        {"keyword": "downlight", "api_key": "do-not-show", "access_token": "do-not-show"}
    ) == {
        "keyword": "downlight",
        "api_key": "[redacted]",
        "access_token": "[redacted]",
    }


def test_web_agent_stream_ignores_partial_tool_call_chunks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakePartialToolCallAgent())
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "开始执行"})

    events = [json.loads(line) for line in response.text.splitlines()]
    tool_events = [event for event in events if event["type"].startswith("tool_")]
    assert {"type": "tool_start", "name": "get_project", "call_id": "project-call"} in tool_events
    assert {"type": "tool_end", "name": "get_project", "call_id": "project-call", "status": "done"} in tool_events
    assert all(event["name"] != "unknown_tool" for event in tool_events)


def test_web_agent_stream_closes_unpaired_tool_call_when_answer_ends(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeUnpairedToolCallAgent())
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "开始执行"})

    events = [json.loads(line) for line in response.text.splitlines()]
    assert {"type": "tool_start", "name": "get_project", "call_id": "unpaired-call"} in events
    assert {"type": "tool_end", "name": "get_project", "call_id": "unpaired-call", "status": "failed"} in events
    assert {"type": "step", "step_id": "project", "status": "failed"} in events


def test_web_chat_stream_emits_fillable_clarification_after_ask_user_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeClarificationAgent())
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "推进照明设计"})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    clarification = next(event for event in events if event["type"] == "clarification")
    assert clarification == {
        "type": "clarification",
        "title": "补充设计条件",
        "question": "请确认以下输入后继续初算。",
        "fields": [
            {
                "field_id": "target_illuminance_lx",
                "label": "目标照度",
                "description": "工作面维持照度。",
                "input_type": "select",
                "required": True,
                "placeholder": None,
                "options": [
                    {"label": "300 lx", "value": "300"},
                    {"label": "500 lx", "value": "500"},
                ],
            }
        ],
    }
    assert events[1]["type"] == "plan"
    assert {"type": "tool_start", "name": "ask_user", "call_id": "clarification-call"} in events
    assert events[-1]["answer"] == "请填写上方条件，我会继续执行。"


def test_clarification_parser_accepts_object_tool_content() -> None:
    class ObjectToolMessage:
        type = "tool"
        name = "ask_user"
        content = {
            "status": "awaiting_user_input",
            "title": "补充条件",
            "question": "请确认。",
            "fields": [],
        }

    assert web_api._clarification_from_tool_chunk(ObjectToolMessage()) == {
        "title": "补充条件",
        "question": "请确认。",
        "fields": [],
    }


def test_web_chat_stream_falls_back_to_a_form_when_model_only_claims_one(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeClaimedClarificationAgent())
    client = make_client(tmp_path)
    project = client.post(
        "/api/projects",
        json={
            "project_name": "Geometry conflict",
            "area_m2": 30,
            "length_m": 6,
            "width_m": 4,
        },
    ).json()

    response = client.post(
        "/api/chat/stream",
        json={"message": "推进设计", "project_id": project["project_id"]},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    clarification = next(event for event in events if event["type"] == "clarification")
    assert clarification["title"] == "补充设计条件"
    assert any(field["field_id"] == "area_m2" for field in clarification["fields"])
    assert events.index(clarification) < len(events) - 1
    assert events[-1]["type"] == "done"


def test_web_agent_stream_pushes_latest_project_after_tool_completion(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeTraceAgent())
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={"project_name": "Agent snapshot"}).json()

    response = client.post(
        "/api/chat/stream",
        json={"message": "开始执行", "mode": "agent", "project_id": project["project_id"]},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    snapshot_event = next(event for event in events if event["type"] == "project")
    assert snapshot_event["project"]["project_id"] == project["project_id"]
    assert snapshot_event["project"]["revision"] == 0


def test_web_chat_stream_exposes_model_reported_context_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: FakeUsageStreamingAgent())
    client = make_client(tmp_path)

    response = client.post("/api/chat/stream", json={"message": "显示上下文窗口"})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert {
        "type": "context",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 8,
            "total_tokens": 128,
            "context_window_tokens": Settings().llm_context_window_tokens,
            "source": "reported",
        },
    } in events


def test_visible_chat_chunk_accepts_provider_final_message() -> None:
    assert web_api._visible_chat_chunk(AIMessage(content="完整回答")) == "完整回答"


def test_web_chat_history_restores_saved_session(tmp_path, monkeypatch) -> None:
    agent = FakeStreamingAgent()
    monkeypatch.setattr(web_api, "build_agent", lambda _settings: agent)
    client = make_client(tmp_path)

    streamed = client.post("/api/chat/stream", json={"message": "请继续分析"})
    session_id = [json.loads(line) for line in streamed.text.splitlines()][0]["session_id"]

    history = client.get(f"/api/chat/{session_id}")
    assert history.status_code == 200
    assert history.json() == {
        "session_id": session_id,
        "messages": [
            {"role": "user", "content": "请继续分析"},
            {"role": "assistant", "content": "## 设计建议\n- 先确认照度目标"},
        ],
    }
