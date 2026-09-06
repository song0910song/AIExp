from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

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
