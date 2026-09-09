from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

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
    gpt_settings = Settings(llm_model="gpt-5.6-terra", llm_api_key="test-key")
    assert gpt_settings.llm_prompt_cache_key == "lighting-design-agent-v1"
    assert gpt_settings.prompt_cache_options() == {"mode": "implicit", "ttl": "30m"}
    assert agent_module._prompt_cache_model_params(gpt_settings) == {
        "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
        "model_kwargs": {"prompt_cache_key": "lighting-design-agent-v1"},
    }

    other_settings = Settings(llm_model="other-model", llm_api_key="test-key")
    assert other_settings.prompt_cache_options() is None
    assert agent_module._prompt_cache_model_params(other_settings) == {}

    explicit_settings = Settings(
        llm_model="other-model",
        llm_api_key="test-key",
        llm_prompt_cache_enabled=True,
        llm_prompt_cache_ttl="unsupported",
    )
    assert explicit_settings.prompt_cache_options() == {"mode": "implicit", "ttl": "30m"}


def test_prompt_cache_uses_a_stable_system_prompt_breakpoint() -> None:
    from langchain_core.messages import SystemMessage

    settings = Settings(llm_model="gpt-5.6-terra", llm_api_key="test-key")
    message = agent_module._system_prompt_for_settings(settings)
    assert isinstance(message, SystemMessage)
    assert message.content[0]["text"] == agent_module.SYSTEM_PROMPT
    assert message.content[0]["prompt_cache_breakpoint"] is True


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
        def invoke(self, _request: dict[str, Any]) -> dict[str, Any]:
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
