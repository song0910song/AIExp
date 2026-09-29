"""Construction and invocation helpers for the constrained ReAct agent."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextvars import ContextVar, Token
from importlib.resources import files
from typing import Any

import httpx

from .config import Settings
from .tools import (
    add_document,
    analyze_dialux_report,
    analyze_dxf_design,
    adopt_evidence,
    apply_rag_lighting_parameters,
    ask_user,
    calculate_preliminary_lighting,
    check_design_rules,
    create_project,
    get_project,
    get_luminaire_detail,
    prepare_luminaire_search,
    propose_relayout,
    propose_retrofit,
    search_evidence,
    search_luminaires,
    select_luminaires,
    send_luminaire_to_dialux,
    update_project_brief,
    verify_illuminance,
)


SYSTEM_PROMPT = files("lighting_agent").joinpath("system_prompt.md").read_text(encoding="utf-8")


# LangGraph propagates contextvars into its model executor threads. A shared
# model can therefore report retries to the request that made the call.
_RETRY_NOTIFIER: ContextVar[Callable[[str], None] | None] = ContextVar(
    "lighting_retry_notifier", default=None
)


class AgentRunCancelled(Exception):
    """A disconnected stream must not start another model or tool call."""


def _raise_if_cancelled(context: Any) -> None:
    if isinstance(context, dict) and (cancel_event := context.get("cancel_event")) is not None:
        if cancel_event.is_set():
            raise AgentRunCancelled()


def _cancelled_run_middleware() -> list[Any]:
    from langchain.agents.middleware import wrap_model_call, wrap_tool_call

    @wrap_model_call
    def guard_model(request: Any, handler: Callable) -> Any:
        _raise_if_cancelled(request.runtime.context)
        return handler(request)

    @wrap_tool_call
    def guard_tool(request: Any, handler: Callable) -> Any:
        _raise_if_cancelled(request.runtime.context)
        return handler(request)

    return [guard_model, guard_tool]


class _RetryNotifyingTransport(httpx.HTTPTransport):
    """Notify the active request each time the SDK is about to retry."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = super().handle_request(request)
        except Exception as error:
            self._notify(f"{error.__class__.__name__}: {error}")
            raise
        if response.status_code in {429} or response.status_code >= 500:
            self._notify(f"HTTP {response.status_code}")
        return response

    @staticmethod
    def _notify(detail: str) -> None:
        notifier = _RETRY_NOTIFIER.get()
        if notifier:
            notifier(detail)


def set_retry_notifier(notifier: Callable[[str], None]) -> Token:
    """Bind a retry callback to this invocation's propagated context."""

    return _RETRY_NOTIFIER.set(notifier)


def reset_retry_notifier(token: Token) -> None:
    _RETRY_NOTIFIER.reset(token)


def _prompt_cache_model_params(settings: Settings) -> dict[str, Any]:
    """Build cache fields accepted by Chat Completions requests."""

    prompt_cache_options = settings.prompt_cache_options()
    if prompt_cache_options is None:
        return {}
    # ChatOpenAI exposes prompt_cache_key through model_kwargs for the Chat
    # Completions API. Keep request-specific data out of this stable key.
    return {
        "prompt_cache_options": prompt_cache_options,
        "model_kwargs": {"prompt_cache_key": settings.llm_prompt_cache_key},
    }


def build_agent(settings: Settings | None = None) -> Any:
    """Build the agent only when an LLM credential is explicitly configured."""

    settings = settings or Settings()
    settings.validate_for_agent()
    from langchain.agents import create_agent
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        reasoning_effort=settings.llm_reasoning_effort,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        stream_usage=True,
        # The configured provider is an OpenAI-compatible Chat Completions
        # gateway; retaining this endpoint preserves its cache protocol.
        use_responses_api=False,
        # Custom http_client keeps _RetryNotifyingTransport; disable
        # langchain-openai's keepalive transport injection so httpx's
        # proxy auto-detection (system proxy) stays active.
        http_socket_options=(),
        http_client=httpx.Client(
            transport=_RetryNotifyingTransport(),
            timeout=settings.llm_timeout_seconds,
        ),
        **_prompt_cache_model_params(settings),
    )
    return create_agent(
        model=model,
        middleware=_cancelled_run_middleware(),
        tools=[
            get_project,
            create_project,
            ask_user,
            update_project_brief,
            apply_rag_lighting_parameters,
            search_evidence,
            adopt_evidence,
            add_document,
            analyze_dxf_design,
            analyze_dialux_report,
            calculate_preliminary_lighting,
            check_design_rules,
            verify_illuminance,
            prepare_luminaire_search,
            search_luminaires,
            get_luminaire_detail,
            propose_relayout,
            propose_retrofit,
            send_luminaire_to_dialux,
            select_luminaires,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


def stream_agent(message: str, *, settings: Settings | None = None) -> Iterator[str]:
    """Yield visible model tokens; tool calls remain available through normal traces."""

    agent = build_agent(settings)
    for chunk in agent.stream({"messages": [{"role": "user", "content": message}]}, stream_mode="messages"):
        if isinstance(chunk, tuple) and getattr(chunk[0], "content", None):
            yield str(chunk[0].content)
