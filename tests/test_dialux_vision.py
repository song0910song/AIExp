"""Vision-model extraction tests for uploaded DIALux screenshots."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import langchain_openai

from lighting_agent.config import Settings
from lighting_agent.dialux_results import analyze_dialux_result_image


def test_analyze_dialux_result_image_sends_multimodal_content_and_parses_json(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            calls["options"] = kwargs

        def invoke(self, messages):
            calls["messages"] = messages
            return SimpleNamespace(
                content="""```json
                {
                  "is_dialux_result": true,
                  "maintained_illuminance_lx": 656,
                  "confidence": 0.97,
                  "metric_label": "E平均 (工作面)",
                  "calculation_surface": "工作面",
                  "explanation": "结果卡片清晰显示 656 lx"
                }
                ```"""
            )

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    settings = replace(
        Settings(),
        llm_api_key="test-key",
        llm_base_url="https://example.test/v1",
        vision_model="test-vision-model",
        vision_timeout_seconds=12,
        vision_max_retries=0,
    )

    analysis = analyze_dialux_result_image(
        b"\x89PNG\r\n\x1a\nresult",
        "image/png",
        settings=settings,
    )

    assert analysis.maintained_illuminance_lx == 656
    assert analysis.confidence == 0.97
    assert analysis.model == "test-vision-model"
    assert calls["options"]["timeout"] == 12
    messages = calls["messages"]
    image_block = messages[0].content[1]
    assert image_block["type"] == "image_url"
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
