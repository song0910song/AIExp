"""A read-only, tool-grounded lighting knowledge assistant."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

from .config import Settings
from .tools import make_tools


SYSTEM_PROMPT = files("lighting_agent").joinpath("system_prompt.md").read_text(encoding="utf-8")


def build_agent(
    settings: Settings | None = None,
    *,
    projects: Any,
    evidence: Any,
    dialux: Any,
    project_id: str | None = None,
) -> Any:
    settings = settings or Settings()
    settings.validate_for_agent()
    from langchain.agents import create_agent
    from langchain_openai import ChatOpenAI

    options: dict[str, Any] = {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key,
        "temperature": settings.llm_temperature,
        "timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
        "use_responses_api": False,
        "stream_usage": True,
        "http_socket_options": (),
    }
    if settings.llm_reasoning_effort is not None:
        options["reasoning_effort"] = settings.llm_reasoning_effort
    cache_options = settings.prompt_cache_options()
    if cache_options is not None:
        options["prompt_cache_options"] = cache_options
        options["model_kwargs"] = {"prompt_cache_key": settings.llm_prompt_cache_key}
    model = ChatOpenAI(**options)
    vision_model = model
    if settings.llm_vision_model and settings.llm_vision_model != settings.llm_model:
        vision_model = ChatOpenAI(**{**options, "model": settings.llm_vision_model})
    return create_agent(
        model=model,
        tools=make_tools(
            projects=projects, evidence=evidence, dialux=dialux, project_id=project_id,
            vision_model=vision_model,
        ),
        system_prompt=SYSTEM_PROMPT,
    )
