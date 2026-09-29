from __future__ import annotations

from threading import Event
from typing import Any
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest

import lighting_agent.agent as agent_module
import lighting_agent.web_api as web_api
from lighting_agent.config import Settings
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.web_api import create_app


def test_settings_normalize_reasoning_efforts() -> None:
    settings = Settings(
        llm_reasoning_efforts=" LOW, medium, low, unsupported ",
        llm_reasoning_effort_default="high",
    )

    assert settings.supported_reasoning_efforts() == ("low", "medium")
    assert settings.default_reasoning_effort() == "low"
    assert settings.with_reasoning_effort("high").llm_reasoning_effort == "high"


def test_prompt_cache_settings_are_stable_and_model_aware() -> None:
    gpt_settings = Settings(
        llm_model="gpt-5.6-terra", llm_base_url="https://api.openai.com/v1",
        llm_api_key="test-key", llm_prompt_cache_enabled=None,
    )
    assert gpt_settings.llm_prompt_cache_key == "lighting-design-agent-v1"
    assert gpt_settings.prompt_cache_options() == {"mode": "implicit", "ttl": "30m"}
    assert agent_module._prompt_cache_model_params(gpt_settings) == {
        "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
        "model_kwargs": {"prompt_cache_key": "lighting-design-agent-v1"},
    }

    gateway_settings = Settings(
        llm_model="gpt-5.6-terra", llm_base_url="https://gateway.example/v1",
        llm_api_key="test-key", llm_prompt_cache_enabled=None,
    )
    assert gateway_settings.prompt_cache_options() is None
    assert agent_module._prompt_cache_model_params(gateway_settings) == {}

    other_settings = Settings(
        llm_model="other-model", llm_base_url="https://api.openai.com/v1",
        llm_api_key="test-key", llm_prompt_cache_enabled=None,
    )
    assert other_settings.prompt_cache_options() is None
    assert agent_module._prompt_cache_model_params(other_settings) == {}

    explicit_settings = Settings(
        llm_model="other-model",
        llm_base_url="https://gateway.example/v1",
        llm_api_key="test-key",
        llm_prompt_cache_enabled=True,
        llm_prompt_cache_ttl="unsupported",
    )
    assert explicit_settings.prompt_cache_options() == {"mode": "implicit", "ttl": "30m"}


@pytest.mark.parametrize("cache_enabled", [None, True])
def test_gateway_request_never_sends_an_explicit_cache_breakpoint(monkeypatch, cache_enabled) -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    monkeypatch.setattr("langchain.agents.create_agent", lambda **kwargs: kwargs)
    model_info = agent_module.build_agent(
        Settings(
            llm_model="gpt-5.6-terra",
            llm_base_url="https://gateway.example/v1",
            llm_api_key="test-key",
            llm_prompt_cache_enabled=cache_enabled,
        )
    )
    assert model_info["system_prompt"] == agent_module.SYSTEM_PROMPT
    payload = model_info["model"]._get_request_payload([
        SystemMessage(content=model_info["system_prompt"]),
        HumanMessage(content="hello"),
    ])
    assert payload["messages"][0] == {"role": "system", "content": agent_module.SYSTEM_PROMPT}
    assert payload.get("prompt_cache_options") == (
        {"mode": "implicit", "ttl": "30m"} if cache_enabled else None
    )
    if cache_enabled is None:
        assert "prompt_cache_key" not in payload


def test_cancelled_run_does_not_start_another_model_or_tool_call() -> None:
    model_guard, tool_guard = agent_module._cancelled_run_middleware()
    cancel_event = Event()
    request = SimpleNamespace(runtime=SimpleNamespace(context={"cancel_event": cancel_event}))
    calls: list[object] = []

    def handler(value: object) -> str:
        calls.append(value)
        return "ok"

    assert model_guard.wrap_model_call(request, handler) == "ok"
    assert tool_guard.wrap_tool_call(request, handler) == "ok"
    cancel_event.set()
    with pytest.raises(agent_module.AgentRunCancelled):
        model_guard.wrap_model_call(request, handler)
    with pytest.raises(agent_module.AgentRunCancelled):
        tool_guard.wrap_tool_call(request, handler)
    assert calls == [request, request]


def test_agent_graph_propagates_request_context_to_model_and_tool() -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    notices: list[str] = []
    mutations: list[str] = []

    class ToolCallingModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "test-model"

        def bind_tools(self, _tools: Any, **_kwargs: Any) -> Any:
            return self

        def _generate(self, _messages: Any, **_kwargs: Any) -> ChatResult:
            agent_module._RetryNotifyingTransport._notify("model retry")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="",
                tool_calls=[{"name": "mutate", "args": {}, "id": "mutate-call"}],
            ))])

    @tool
    def mutate() -> str:
        """Record a test mutation."""
        mutations.append("called")
        return "done"

    graph = create_agent(ToolCallingModel(), [mutate], middleware=agent_module._cancelled_run_middleware())
    cancel_event = Event()
    token = agent_module.set_retry_notifier(notices.append)
    try:
        chunks = graph.stream(
            {"messages": [{"role": "user", "content": "test"}]},
            stream_mode="messages",
            context={"cancel_event": cancel_event},
        )
        next(chunks)
        cancel_event.set()
        with pytest.raises(agent_module.AgentRunCancelled):
            list(chunks)
    finally:
        agent_module.reset_retry_notifier(token)

    assert notices == ["model retry"]
    assert mutations == []


def test_health_lists_reasoning_effort_options(tmp_path) -> None:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )

    payload = TestClient(app).get("/api/health").json()

    assert payload["llm_reasoning_efforts"] == ["none", "low", "medium", "high"]
    assert payload["llm_reasoning_effort_default"] == "medium"
    assert [item["value"] for item in payload["llm_reasoning_effort_options"]] == [
        "none",
        "low",
        "medium",
        "high",
    ]


def test_chat_builds_an_agent_for_each_selected_effort(tmp_path, monkeypatch) -> None:
    settings = Settings(
        llm_api_key="test-key",
        llm_model="test-model",
        llm_reasoning_efforts="none,low,medium,high",
    )
    monkeypatch.setattr(web_api, "Settings", lambda: settings)
    captured: list[str | None] = []

    class FakeAgent:
        def invoke(self, _request: dict[str, Any], *, config: dict[str, Any]) -> dict[str, Any]:
            assert config["recursion_limit"] == max(4, settings.agent_max_steps)
            return {"messages": [AIMessage(content="ok")]}

    def build(captured_settings: Settings) -> FakeAgent:
        captured.append(captured_settings.llm_reasoning_effort)
        return FakeAgent()

    monkeypatch.setattr(web_api, "build_agent", build)
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )
    client = TestClient(app)

    assert client.post("/api/chat", json={"message": "one", "reasoning_effort": "high"}).status_code == 200
    assert client.post("/api/chat", json={"message": "two", "reasoning_effort": "low"}).status_code == 200
    assert client.post("/api/chat", json={"message": "three"}).status_code == 200
    assert client.post("/api/chat", json={"message": "four", "reasoning_effort": "high"}).status_code == 200
    assert captured == ["high", "low", None]

    invalid = client.post("/api/chat", json={"message": "bad", "reasoning_effort": "xhigh"})
    assert invalid.status_code == 422
