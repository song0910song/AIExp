"""FastAPI adapter for the existing lighting-design domain services.

The web layer deliberately contains no lighting calculations or compliance
rules. It exposes the same ProjectStore, RAG, DIALux and deliverable services
used by the CLI and LangChain tools.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .agent import build_agent, set_retry_notifier
from .calculations import calculate_lumen_method, check_design_rules
from .config import Settings, USER_DOCUMENTS_DIRECTORY, ensure_data_directories
from .deliverables import build_design_report, build_dialux_task_archive, read_dialux_task_package
from .dialux_api import DialuxAPI, DialuxAPIError, validate_luminaire_search
from .document_loader import DocumentLoadError, load_document
from .floor_plan import MAX_DRAWING_BYTES, FloorPlanParseError, parse_floor_plan
from .project_store import ProjectNotFoundError, ProjectStore, RevisionConflictError
from .photometry_assets import PhotometryAssetStore
from .rag import EvidenceNotFoundError, create_evidence_store, format_evidence
from .schemas import (
    CalculationInput,
    DesignBrief,
    LuminaireSearchRequest,
    ProjectState,
    ProjectUpdate,
    RuleRequirement,
    SimulationMetrics,
    SimulationRun,
    StrictModel,
)
from .storage import SQLiteDatabase


class BriefUpdateRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    brief: DesignBrief


class CalculationRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    inputs: CalculationInput


class RuleCheckRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    requirements: list[RuleRequirement] = Field(min_length=1, max_length=20)
    observations: dict[str, float | int | None]


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)


class EvidenceAdoptionRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class LuminaireWebRequest(LuminaireSearchRequest):
    expected_revision: int | None = Field(default=None, ge=0)
    save_to_project: bool = True


class LuminaireSelectionRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    luminaire_ids: list[str] = Field(default_factory=list, max_length=100)


class DialuxResultRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    handoff_id: str = Field(min_length=8, max_length=128)
    input_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metrics: SimulationMetrics
    source_kind: Literal["dialux_pdf", "dialux_csv", "dialux_json", "manual_form"] = "manual_form"
    solver_version: str | None = Field(default=None, max_length=120)
    parser_version: str = Field(default="manual-form-1", max_length=80)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, min_length=8, max_length=64)
    project_id: str | None = Field(default=None, min_length=8, max_length=64)
    debug: bool = False
    # Retained for existing API clients.
    mode: Literal["chat", "agent"] = "chat"


class ChatSessionStore:
    """SQLite-backed LangChain message history with expiry and a bounded size."""

    def __init__(self, database_path: Path, settings: Settings | None = None) -> None:
        self.database = SQLiteDatabase(database_path)
        self.settings = settings or Settings()

    def get(self, session_id: str) -> list[Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT messages_json, expires_at FROM chat_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return []
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(UTC):
                connection.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
                return []
            return self._decode(str(row["messages_json"]))
        finally:
            connection.close()

    def save(self, session_id: str, messages: list[Any], *, project_id: str | None = None) -> None:
        normalized = self._normalize(messages)
        maximum = max(1, self.settings.chat_session_max_messages)
        payload = self._encode(normalized[-maximum:])
        now = datetime.now(UTC)
        expires = now + timedelta(hours=max(1, self.settings.chat_session_ttl_hours))
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_sessions (session_id, project_id, messages_json, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_id = COALESCE(excluded.project_id, chat_sessions.project_id),
                    messages_json = excluded.messages_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (session_id, project_id, payload, now.isoformat(), now.isoformat(), expires.isoformat()),
            )

    def clear(self, session_id: str) -> None:
        connection = self.database.connect()
        try:
            connection.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
        finally:
            connection.close()

    @staticmethod
    def _normalize(messages: list[Any]) -> list[Any]:
        from langchain_core.messages import convert_to_messages

        return list(convert_to_messages(messages))

    @classmethod
    def _encode(cls, messages: list[Any]) -> str:
        from langchain_core.messages import messages_to_dict

        return json.dumps(messages_to_dict(messages), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(payload: str) -> list[Any]:
        from langchain_core.messages import messages_from_dict

        return list(messages_from_dict(json.loads(payload)))


def _event_line(event: dict[str, Any]) -> str:
    """Encode one newline-delimited JSON event for the chat stream."""

    return json.dumps(event, ensure_ascii=False) + "\n"


def _stream_text(content: Any) -> str:
    """Extract displayable text from a LangChain streaming message chunk."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    return ""


def _visible_chat_chunk(chunk: Any) -> str:
    """Return text only for assistant token chunks, never for tool output."""

    message = chunk[0] if isinstance(chunk, tuple) else chunk
    # Some OpenAI-compatible providers emit one complete AIMessage instead
    # of incremental AIMessageChunk objects. Both are safe to display.
    if message.__class__.__name__ not in {"AIMessageChunk", "AIMessage"}:
        return ""
    return _stream_text(getattr(message, "content", None))


def _clarification_from_tool_chunk(chunk: Any) -> dict[str, Any] | None:
    """Extract a structured ask_user result from a LangChain ToolMessage."""

    message = _stream_message(chunk)
    if getattr(message, "type", "") != "tool" or getattr(message, "name", "") != "ask_user":
        return None
    content = getattr(message, "content", "")
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None
    else:
        return None
    if not isinstance(payload, dict) or payload.get("status") != "awaiting_user_input":
        return None
    if not isinstance(payload.get("title"), str) or not isinstance(payload.get("question"), str):
        return None
    if not isinstance(payload.get("fields"), list):
        return None
    return {
        "title": payload["title"],
        "question": payload["question"],
        "fields": payload["fields"],
    }


def _fallback_clarification(project: ProjectState) -> dict[str, Any]:
    """Create a usable form when a model claims it created one without calling ask_user."""

    brief = project.brief
    fields: list[dict[str, Any]] = []

    def add_field(
        field_id: str,
        label: str,
        description: str,
        input_type: str = "text",
        *,
        placeholder: str | None = None,
        options: list[dict[str, str]] | None = None,
    ) -> None:
        if len(fields) >= 6 or any(item["field_id"] == field_id for item in fields):
            return
        fields.append(
            {
                "field_id": field_id,
                "label": label,
                "description": description,
                "input_type": input_type,
                "required": True,
                "placeholder": placeholder,
                "options": options or [],
            }
        )

    if brief.area_m2 is not None and brief.length_m is not None and brief.width_m is not None:
        calculated_area = brief.length_m * brief.width_m
        if abs(brief.area_m2 - calculated_area) > max(0.5, calculated_area * 0.03):
            add_field(
                "area_m2",
                "确认最终设计面积（m2）",
                f"当前面积为 {brief.area_m2:g} m2，而长 x 宽为 {calculated_area:g} m2；请填写计算和选型应采用的最终面积。",
                "number",
                placeholder=f"{brief.area_m2:g}",
            )

    missing_fields = {
        "space_type": ("空间类型", "请填写空间用途，例如会议室、教室或走廊。", "text"),
        "area_m2": ("设计面积（m2）", "请填写实际参与照明计算的面积。", "number"),
        "target_illuminance_lx": ("目标照度（lx）", "请填写工作面维持照度目标。", "number"),
        "mounting_height_m": ("灯具安装高度（m）", "请填写灯具发光面距完成地面的安装高度。", "number"),
    }
    for field_id in brief.missing_design_inputs():
        label, description, input_type = missing_fields[field_id]
        add_field(field_id, label, description, input_type)

    suggested_fields: tuple[tuple[str, str, str, str, str | None, list[dict[str, str]]], ...] = (
        ("room_height_m", "确认房间净高（m）", "当前值需要用户确认后才能用于空间几何建模。", "number", str(brief.room_height_m) if brief.room_height_m is not None else None, []),
        ("target_cct_k", "确认目标色温（K）", "请确认灯具的目标相关色温。", "select", str(brief.target_cct_k) if brief.target_cct_k is not None else None, [{"label": "3000 K", "value": "3000"}, {"label": "3500 K", "value": "3500"}, {"label": "4000 K", "value": "4000"}, {"label": "5000 K", "value": "5000"}]),
        ("min_cri", "确认最低显色指数（Ra）", "请确认设计与灯具筛选采用的最低显色指数。", "select", str(brief.min_cri) if brief.min_cri is not None else None, [{"label": "Ra 80", "value": "80"}, {"label": "Ra 90", "value": "90"}, {"label": "Ra 95", "value": "95"}]),
        ("target_ugr", "确认 UGR 上限", "请确认眩光控制目标。", "number", str(brief.target_ugr) if brief.target_ugr is not None else None, []),
        ("target_uniformity_u0", "确认最低均匀度 U0", "请确认照度均匀度目标。", "number", str(brief.target_uniformity_u0) if brief.target_uniformity_u0 is not None else None, []),
        ("max_lpd_w_m2", "确认照明功率密度上限（W/m2）", "如有节能控制要求，请填写项目采用的 LPD 上限。", "number", str(brief.max_lpd_w_m2) if brief.max_lpd_w_m2 is not None else None, []),
    )
    for field_id, label, description, input_type, placeholder, options in suggested_fields:
        if field_id not in brief.confirmed_fields:
            add_field(field_id, label, description, input_type, placeholder=placeholder, options=options)

    if not fields:
        add_field(
            "design_conditions",
            "待确认设计条件",
            "请填写智能体在本轮中要求确认的条件，以便继续执行。",
            placeholder="请输入确认结果",
        )

    return {
        "title": "补充设计条件",
        "question": "请确认以下条件后继续执行。本表单由系统根据项目事实生成，提交后会作为本轮确认信息发送给智能体。",
        "fields": fields,
    }


def _claims_structured_clarification(answer: str) -> bool:
    """Detect an invalid natural-language claim that a fillable form already exists."""

    return any(marker in answer for marker in ("结构化询问", "结构化提问", "已生成问询", "填写后继续", "请填写后继续"))


def _token_count(value: Any) -> int | None:
    """Normalize one provider token-count field without accepting invalid data."""

    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _first_token_count(metadata: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        count = _token_count(metadata.get(name))
        if count is not None:
            return count
    return None


def _context_usage_from_chunk(chunk: Any, context_window_tokens: int) -> dict[str, Any] | None:
    """Extract normalized usage from OpenAI-compatible LangChain message chunks."""

    message = _stream_message(chunk)
    if message.__class__.__name__ not in {"AIMessageChunk", "AIMessage"}:
        return None

    candidates: list[dict[str, Any]] = []
    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        candidates.append(usage_metadata)
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        for key in ("token_usage", "usage"):
            nested = response_metadata.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        candidates.append(response_metadata)

    for metadata in candidates:
        input_tokens = _first_token_count(
            metadata,
            ("input_tokens", "prompt_tokens", "prompt_token_count", "input_token_count"),
        )
        output_tokens = _first_token_count(
            metadata,
            ("output_tokens", "completion_tokens", "completion_token_count", "output_token_count"),
        )
        total_tokens = _first_token_count(metadata, ("total_tokens", "total_token_count"))
        if input_tokens is None and output_tokens is None and total_tokens is None:
            continue
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "context_window_tokens": max(1, context_window_tokens),
            "source": "reported",
        }
    return None


def _chat_error_detail(error: Exception, settings: Settings) -> str:
    """Make upstream model failures actionable in the browser."""

    text = str(error).strip()
    error_name = error.__class__.__name__.casefold()
    if "timeout" in error_name or "timeout" in text.casefold():
        return (
            f"模型服务在 {settings.llm_timeout_seconds:g} 秒内未响应。"
            "请检查 LIGHTING_LLM_BASE_URL、模型名称、密钥和网络连接后重试。"
        )
    return text or "聊天流式响应失败"


def _chat_history(messages: list[Any]) -> list[dict[str, str]]:
    """Return display-safe user and assistant messages from LangChain history."""

    history: list[dict[str, str]] = []
    for message in messages:
        message_type = getattr(message, "type", "")
        if message_type in {"human", "user"}:
            role = "user"
        elif message_type in {"ai", "assistant"}:
            role = "assistant"
        else:
            # System, tool and internal messages remain available to the
            # agent, but should not be rendered as part of a user transcript.
            continue
        content = _stream_text(getattr(message, "content", None))
        # Older web sessions embedded the project revision in each user
        # message. Keep the actual question, but do not let an obsolete
        # revision stay in the model's conversational history.
        if role == "user" and "project_id" in content and "revision" in content and "\n\n" in content:
            content = content.rsplit("\n\n", maxsplit=1)[-1]
        if content:
            history.append({"role": role, "content": content})
    return history


def _project_chat_content(message: str, project_id: str, revision: int) -> str:
    """Provide one-turn project context without persisting a revision in history."""

    return (
        f"Current project_id is {project_id}; its authoritative current revision is {revision}.\n"
        "Before reading or changing project data, call get_project for this project. "
        "For every write, use the revision returned by the latest get_project or mutating tool result; "
        "never reuse a revision from an earlier conversation turn.\n\n"
        f"User question:\n{message}"
    )


_AGENT_WORKFLOW_STEPS: tuple[dict[str, Any], ...] = (
    {
        "id": "project",
        "title": "读取项目现状",
        "description": "读取任务书、已有证据、计算、灯具与当前 revision。",
        "tools": ["get_project"],
    },
    {
        "id": "evidence",
        "title": "检索并采纳规范证据",
        "description": "从已审批资料库检索原文，并将采纳的证据写入项目。",
        "tools": ["search_evidence", "adopt_evidence", "add_document"],
    },
    {
        "id": "brief",
        "title": "补齐设计条件",
        "description": "优先使用已确认 CAD 平面图、规范与项目资料补齐设计条件；仅在证据不确定时请求确认。",
        "tools": ["apply_rag_lighting_parameters", "ask_user", "update_project_brief"],
    },
    {
        "id": "calculation",
        "title": "初算与规则校核",
        "description": "运行流明法初算，并只按已明确的规则进行确定性校核。",
        "tools": ["calculate_preliminary_lighting", "check_design_rules"],
    },
    {
        "id": "luminaires",
        "title": "筛选 DIALux 灯具",
        "description": "按已确认的参数检索候选灯具，并在用户确认后标记最终型号。",
        "tools": ["prepare_luminaire_search", "search_luminaires", "get_luminaire_detail", "select_luminaires"],
    },
    {
        "id": "deliverables",
        "title": "生成交付物",
        "description": "生成设计报告或 DIALux evo 任务包；仿真复核仍需在 DIALux evo 完成。",
        "tools": ["generate_design_report", "create_dialux_task_package"],
    },
)

_AGENT_TOOL_STEPS = {
    tool_name: step["id"]
    for step in _AGENT_WORKFLOW_STEPS
    for tool_name in step["tools"]
}


def _agent_plan() -> list[dict[str, Any]]:
    """Return the fixed, auditable workflow shown before an agent run."""

    return [{**step, "status": "pending"} for step in _AGENT_WORKFLOW_STEPS]


def _stream_message(chunk: Any) -> Any:
    return chunk[0] if isinstance(chunk, tuple) else chunk


def _tool_name(value: Any) -> str | None:
    """Return a usable tool name, never a placeholder from a partial chunk."""

    name = str(value or "").strip()
    return name or None


def _tool_call_id(value: Any) -> str | None:
    """Return a usable provider call id without fabricating one for partial data."""

    call_id = str(value or "").strip()
    return call_id or None


def _debug_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, secret-safe representation for the browser trace."""

    if depth >= 2:
        return "…"
    if isinstance(value, str):
        normalized = value.strip()
        return normalized[:160] + ("…" if len(normalized) > 160 else "")
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        preview = [_debug_value(item, depth=depth + 1) for item in value[:8]]
        if len(value) > 8:
            preview.append(f"… ({len(value) - 8} more)")
        return preview
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:16]:
            name = str(key)
            normalized_name = name.casefold().replace("-", "_")
            sensitive = (
                normalized_name in {"key", "secret", "token", "password", "authorization"}
                or normalized_name.endswith(("_key", "_secret", "_token", "_password"))
            )
            if sensitive:
                result[name] = "[redacted]"
            else:
                result[name] = _debug_value(item, depth=depth + 1)
        if len(value) > 16:
            result["…"] = f"{len(value) - 16} more fields"
        return result
    return str(value)[:160]


def _debug_tool_result(content: Any) -> dict[str, Any]:
    """Summarize tool output without publishing documents or vendor raw fields."""

    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return {"text": _debug_value(content)}
    else:
        payload = content
    if not isinstance(payload, dict):
        return {"value": _debug_value(payload)}

    summary: dict[str, Any] = {}
    for key in (
        "status",
        "project_id",
        "project_revision",
        "revision",
        "saved_count",
        "rebased",
        "message",
        "task_package",
        "report",
        "source",
    ):
        if key in payload:
            summary[key] = _debug_value(payload[key])
    for key in (
        "missing_fields",
        "missing_requested_fields",
        "failed_requested_fields",
        "warnings",
        "applied_fields",
        "evidence_ids",
    ):
        if key in payload:
            summary[key] = _debug_value(payload[key])
    for key in ("candidates", "evidence", "checks", "calculations", "fields", "assets"):
        value = payload.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    if not summary:
        summary["returned_fields"] = sorted(str(key) for key in payload)[:20]
    return summary


def _tool_calls_from_chunk(chunk: Any, *, include_debug: bool = False) -> list[dict[str, Any]]:
    """Normalize complete streamed AI tool calls.

    OpenAI-compatible providers can send an AIMessageChunk before its tool
    name has been decoded. Publishing that partial chunk creates a synthetic
    ``unknown_tool`` with no matching ToolMessage, so wait for both fields.
    """

    message = _stream_message(chunk)
    calls = getattr(message, "tool_calls", None) or []
    normalized: list[dict[str, str]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = _tool_name(call.get("name"))
        call_id = _tool_call_id(call.get("id"))
        if not name or not call_id:
            continue
        event: dict[str, Any] = {"name": name, "call_id": call_id}
        if include_debug:
            event["input"] = _debug_value(call.get("args", {}))
        normalized.append(event)
    return normalized


def _tool_result_from_chunk(chunk: Any, *, include_debug: bool = False) -> dict[str, Any] | None:
    """Return one completed tool event when LangGraph yields a ToolMessage."""

    message = _stream_message(chunk)
    if getattr(message, "type", "") != "tool":
        return None
    call_id = _tool_call_id(getattr(message, "tool_call_id", None))
    if not call_id:
        return None
    name = _tool_name(getattr(message, "name", None))
    status = "failed" if getattr(message, "status", "success") == "error" else "done"
    event: dict[str, Any] = {"name": name, "call_id": call_id, "status": status}
    if include_debug:
        event["output"] = _debug_tool_result(getattr(message, "content", ""))
    return event


def create_app(
    *,
    project_store: ProjectStore | None = None,
    evidence_store: Any | None = None,
    dialux_api: DialuxAPI | None = None,
) -> FastAPI:
    ensure_data_directories()
    projects = project_store or ProjectStore()
    evidence = evidence_store or create_evidence_store()
    dialux = dialux_api or DialuxAPI()
    settings = Settings()
    photometry_assets = PhotometryAssetStore(projects.directory, dialux)
    sessions = ChatSessionStore(projects.database_path)
    agent_holder: dict[str, Any] = {}

    app = FastAPI(
        title="照明设计智能体 API",
        version="0.1.0",
        description="The HTTP adapter for the auditable lighting-design services.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ProjectNotFoundError)
    async def project_not_found_handler(_, error: ProjectNotFoundError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RevisionConflictError)
    async def revision_conflict_handler(_, error: RevisionConflictError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_configured": bool(settings.llm_api_key),
            "llm_model": settings.llm_model,
            "rag_backend": settings.rag_backend,
            "llm_context_window_tokens": max(1, settings.llm_context_window_tokens),
            "project_count": len(projects.list()),
        }

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        states = sorted(projects.list(), key=lambda item: item.updated_at, reverse=True)
        return [state.model_dump(mode="json") for state in states]

    @app.post("/api/projects", status_code=201)
    def create_project(brief: DesignBrief) -> dict[str, Any]:
        return projects.create(brief).model_dump(mode="json")

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return projects.get(project_id).model_dump(mode="json")

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str) -> None:
        projects.delete(project_id)
        photometry_assets.remove_project(project_id)

    @app.get("/api/projects/{project_id}/revisions")
    def get_project_revisions(project_id: str) -> list[dict[str, Any]]:
        return [state.model_dump(mode="json") for state in projects.revisions(project_id)]

    @app.get("/api/projects/{project_id}/dialux-results")
    def list_dialux_results(project_id: str) -> list[dict[str, Any]]:
        state = projects.get(project_id)
        return [item.model_dump(mode="json") for item in state.simulation_runs]

    @app.get("/api/projects/{project_id}/dialux-results/{run_id}")
    def get_dialux_result(project_id: str, run_id: str) -> dict[str, Any]:
        try:
            run = projects.get_simulation_run(project_id, run_id)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return run.model_dump(mode="json")

    @app.post("/api/projects/{project_id}/dialux-results", status_code=201)
    def import_dialux_result(project_id: str, request: DialuxResultRequest) -> dict[str, Any]:
        state = projects.get(project_id)
        handoff_path = projects.artifact_path(project_id, ".dialux-task.zip")
        if not handoff_path.exists():
            raise HTTPException(status_code=404, detail="请先生成 DIALux 任务包")
        try:
            package = read_dialux_task_package(handoff_path.read_bytes())
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=f"无法读取 DIALux 任务包：{error}") from error

        messages: list[str] = []
        expected_snapshot = package.get("input_snapshot_sha256")
        package_handoff_id = package.get("handoff_id")
        if request.handoff_id != package_handoff_id:
            messages.append("handoff_id 与当前任务包不匹配")
        if request.input_snapshot_sha256 and request.input_snapshot_sha256 != expected_snapshot:
            messages.append("input_snapshot_sha256 与当前任务包不匹配")
        if state.revision != request.expected_revision:
            raise RevisionConflictError(
                f"Project revision is {state.revision}, but request expected {request.expected_revision}"
            )

        package_snapshot = package.get("input_snapshot", {})
        if package_snapshot.get("project_id") != project_id:
            messages.append("任务包不属于当前项目")
        if package_snapshot.get("selected_luminaire_ids", []) != state.selected_luminaire_ids:
            messages.append("任务包中的最终灯具与当前项目不一致")
        if package_snapshot.get("project_revision") != request.expected_revision:
            messages.append("任务包中的项目 revision 与导入 revision 不一致")

        status = "matched" if not messages else "mismatch"
        run = SimulationRun(
            kind="精算",
            status="succeeded" if status == "matched" else "unverified",
            input_project_revision=request.expected_revision,
            solver_version=request.solver_version,
            handoff_id=request.handoff_id,
            input_snapshot_sha256=request.input_snapshot_sha256 or expected_snapshot,
            selected_luminaire_ids=list(package.get("selected_luminaire_ids", [])),
            photometry_sha256_by_luminaire=dict(package.get("photometry_sha256_by_luminaire", {})),
            source_kind=request.source_kind,
            metrics=request.metrics,
            verification_status=status,
            verification_messages=messages,
            parser_version=request.parser_version,
            completed_at=datetime.now(UTC),
        )
        updated = projects.append_simulation_run(project_id, request.expected_revision, run)
        return {"simulation_run": run.model_dump(mode="json"), "project": updated.model_dump(mode="json")}

    @app.put("/api/projects/{project_id}/brief")
    def update_brief(project_id: str, request: BriefUpdateRequest) -> dict[str, Any]:
        state = projects.update(
            project_id,
            ProjectUpdate(expected_revision=request.expected_revision, brief=request.brief),
        )
        return state.model_dump(mode="json")

    @app.post("/api/evidence/search")
    def search_evidence(request: EvidenceSearchRequest) -> dict[str, Any]:
        results = evidence.search(request.query, top_k=request.top_k)
        return {
            "evidence": [item.model_dump(mode="json") for item in results],
            "formatted": format_evidence(results),
        }

    @app.post("/api/projects/{project_id}/evidence")
    def adopt_evidence(project_id: str, request: EvidenceAdoptionRequest) -> dict[str, Any]:
        try:
            adopted = evidence.get_evidence(request.evidence_ids)
        except EvidenceNotFoundError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        state = projects.get(project_id)
        existing_ids = {item.evidence_id for item in state.evidence}
        merged = [*state.evidence, *(item for item in adopted if item.evidence_id not in existing_ids)]
        updated = projects.update(
            project_id,
            ProjectUpdate(expected_revision=request.expected_revision, evidence=merged),
        )
        return {
            "evidence": [item.model_dump(mode="json") for item in adopted],
            "project": updated.model_dump(mode="json"),
        }

    @app.post("/api/documents", status_code=201)
    async def add_document(
        file: Annotated[UploadFile, File()],
        source_type: Annotated[Literal["standard", "project_document", "user_note"], Form()] = "project_document",
    ) -> dict[str, Any]:
        safe_name = _safe_upload_name(file.filename or "document")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in {".pdf", ".docx", ".md", ".txt"}:
            raise HTTPException(status_code=415, detail="仅支持 .pdf、.docx、.md 和 .txt 文件")
        content = await file.read()
        if len(content) > MAX_DRAWING_BYTES:
            raise HTTPException(status_code=413, detail="文件不能超过 50 MB")
        target = _unique_upload_target(USER_DOCUMENTS_DIRECTORY, safe_name)
        await run_in_threadpool(target.write_bytes, content)

        def index_upload():
            document = load_document(target)
            return document, evidence.add_document(document, source_type=source_type)

        try:
            document, chunk_count = await run_in_threadpool(index_upload)
        except DocumentLoadError as error:
            await run_in_threadpool(target.unlink, missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "source_name": document.source_name,
            "source_type": source_type,
            "sha256": document.sha256,
            "page_count": document.page_count,
            "indexed_chunks": chunk_count,
        }

    @app.post("/api/projects/{project_id}/calculations")
    def calculate(project_id: str, request: CalculationRequest) -> dict[str, Any]:
        result = calculate_lumen_method(request.inputs)
        state = projects.get(project_id)
        updated = projects.update(
            project_id,
            ProjectUpdate(
                expected_revision=request.expected_revision,
                calculations=[*state.calculations, result],
            ),
        )
        return {
            "calculation": result.model_dump(mode="json"),
            "project": updated.model_dump(mode="json"),
        }

    @app.get("/api/projects/{project_id}/floor-plan")
    def get_floor_plan(project_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        if state.floor_plan is None:
            raise HTTPException(status_code=404, detail="项目尚未导入平面图")
        return state.floor_plan.model_dump(mode="json")

    @app.post("/api/projects/{project_id}/floor-plan", status_code=201)
    async def import_floor_plan(
        project_id: str,
        file: Annotated[UploadFile, File()],
        expected_revision: Annotated[int, Form(ge=0)],
    ) -> dict[str, Any]:
        projects.get(project_id)
        safe_name = _safe_upload_name(file.filename or "floor-plan.dxf")
        suffix = Path(safe_name).suffix.casefold()
        if suffix not in {".dxf", ".dwg"}:
            raise HTTPException(status_code=415, detail="仅支持 .dxf 与 .dwg 平面图文件")
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="平面图文件不能超过 50 MB")
        plans_directory = projects.directory / f"{project_id}.plans"
        target = _unique_upload_target(plans_directory, safe_name)
        await run_in_threadpool(target.write_bytes, content)
        storage_path = str(target.relative_to(projects.directory).as_posix())
        try:
            floor_plan = await run_in_threadpool(
                parse_floor_plan,
                target,
                storage_path=storage_path,
            )
        except FloorPlanParseError as error:
            await run_in_threadpool(target.unlink, missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error
        candidate_index = next(
            (index for index, candidate in enumerate(floor_plan.area_candidates) if candidate.area_m2 is not None),
            None,
        )
        try:
            updated = projects.set_floor_plan(
                project_id,
                expected_revision,
                floor_plan,
                candidate_index,
            )
        except RevisionConflictError:
            await run_in_threadpool(target.unlink, missing_ok=True)
            raise
        except (IndexError, ValueError) as error:
            await run_in_threadpool(target.unlink, missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "floor_plan": floor_plan.model_dump(mode="json"),
            "project": updated.model_dump(mode="json"),
            "applied_area_candidate_index": candidate_index,
        }

    @app.post("/api/projects/{project_id}/rule-checks")
    def rules(project_id: str, request: RuleCheckRequest) -> dict[str, Any]:
        checks = check_design_rules(request.requirements, request.observations)
        state = projects.get(project_id)
        updated = projects.update(
            project_id,
            ProjectUpdate(
                expected_revision=request.expected_revision,
                rule_checks=[*state.rule_checks, *checks],
            ),
        )
        return {
            "checks": [item.model_dump(mode="json") for item in checks],
            "project": updated.model_dump(mode="json"),
        }

    @app.post("/api/projects/{project_id}/luminaires")
    def luminaires(project_id: str, request: LuminaireWebRequest) -> dict[str, Any]:
        state = projects.get(project_id)
        search_request = LuminaireSearchRequest.model_validate(
            request.model_dump(exclude={"expected_revision", "save_to_project"})
        )
        search_request, missing = validate_luminaire_search(search_request, state.brief)
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "missing_search_conditions",
                    "message": "DIALux search requires confirmed room/use and at least one key lighting condition (illuminance/CCT/CRI/UGR).",
                    "missing_fields": missing,
                },
            )
        try:
            candidates = dialux.search(search_request)
            search_run = None
        except DialuxAPIError as error:
            raise HTTPException(status_code=502, detail=error.as_dict()) from error
        payload: dict[str, Any] = {
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "search_run": search_run.model_dump(mode="json") if search_run is not None else None,
            "notice": "候选灯具需在 DIALux evo 中核验照度、均匀度与 UGR。",
        }
        if request.save_to_project:
            if request.expected_revision is None:
                raise HTTPException(status_code=422, detail="保存候选灯具时必须提供 expected_revision")
            updated, saved_count, rebased = projects.append_luminaires(
                project_id,
                request.expected_revision,
                candidates,
                search_run,
            )
            saved_by_id = {item.luminaire_id: item for item in updated.luminaires}
            returned_ids = [item.luminaire_id for item in candidates]
            payload["candidates"] = [
                saved_by_id[luminaire_id].model_dump(mode="json")
                for luminaire_id in returned_ids
                if luminaire_id in saved_by_id
            ]
            payload["saved_candidate_ids"] = [
                luminaire_id for luminaire_id in returned_ids if luminaire_id in saved_by_id
            ]
            excluded_ids = [
                luminaire_id for luminaire_id in returned_ids if luminaire_id not in saved_by_id
            ]
            if excluded_ids:
                payload["excluded_candidate_ids"] = excluded_ids
            payload["project"] = updated.model_dump(mode="json")
            payload["saved_count"] = saved_count
            payload["rebased"] = rebased
        return payload

    @app.get("/api/projects/{project_id}/luminaires/{luminaire_id}")
    def luminaire_detail(project_id: str, luminaire_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        candidate = next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Luminaire is not in this project")
        return {
            "candidate": candidate.model_dump(mode="json"),
            "untrusted_supplier_data": True,
        }

    @app.put("/api/projects/{project_id}/selected-luminaires")
    def select_luminaires(project_id: str, request: LuminaireSelectionRequest) -> dict[str, Any]:
        try:
            state = projects.set_selected_luminaires(
                project_id,
                request.expected_revision,
                request.luminaire_ids,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return state.model_dump(mode="json")

    @app.get("/api/projects/{project_id}/photometry")
    def list_photometry_assets(project_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        return {
            "assets": [item.model_dump(mode="json") for item in photometry_assets.list_assets(state)],
        }

    @app.post("/api/projects/{project_id}/luminaires/{luminaire_id}/photometry")
    def download_luminaire_photometry(project_id: str, luminaire_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        if luminaire_id not in state.selected_luminaire_ids:
            raise HTTPException(
                status_code=422,
                detail="请先将灯具设为最终选定项；仅最终选定灯具可下载配光数据",
            )
        try:
            asset = photometry_assets.download(state, luminaire_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"asset": asset.model_dump(mode="json")}

    @app.get("/api/projects/{project_id}/luminaires/{luminaire_id}/photometry/file")
    def download_saved_luminaire_photometry(project_id: str, luminaire_id: str):
        state = projects.get(project_id)
        if luminaire_id not in state.selected_luminaire_ids:
            raise HTTPException(
                status_code=404,
                detail="该灯具不是当前最终选定项，配光文件不可下载",
            )
        asset = next(
            (item for item in photometry_assets.list_assets(state) if item.luminaire_id == luminaire_id),
            None,
        )
        if asset is None or asset.status != "downloaded" or asset.zip_file is None:
            raise HTTPException(status_code=404, detail="Photometry ZIP has not been downloaded for this luminaire")
        try:
            target = photometry_assets.read_file(project_id, asset.zip_file)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="Saved photometry ZIP is missing") from error
        return FileResponse(target, media_type="application/zip", filename=target.name)

    @app.get("/api/projects/{project_id}/luminaires/{luminaire_id}/photometry/extracted")
    def download_extracted_luminaire_photometry(
        project_id: str,
        luminaire_id: str,
        relative_path: str,
    ):
        """Download one verified ULD/IES/LDT file for manual DIALux import.

        File-association dispatch cannot prove that DIALux changed the active
        project.  This endpoint makes the exact verified source file available
        to the operator so that the import can be performed and confirmed in
        the DIALux user interface.
        """

        state = projects.get(project_id)
        if luminaire_id not in state.selected_luminaire_ids:
            raise HTTPException(status_code=404, detail="该灯具不是当前最终选定项，配光文件不可下载")
        asset = next(
            (item for item in photometry_assets.list_assets(state) if item.luminaire_id == luminaire_id),
            None,
        )
        extracted = next(
            (item for item in (asset.extracted_files if asset is not None else []) if item.relative_path == relative_path),
            None,
        )
        if extracted is None:
            raise HTTPException(status_code=404, detail="未找到该灯具的已验证配光文件")
        try:
            target = photometry_assets.read_file(project_id, extracted.relative_path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="已保存的配光文件不存在") from error
        media_type = "application/octet-stream" if extracted.file_type == "uld" else "text/plain"
        return FileResponse(target, media_type=media_type, filename=target.name)

    @app.delete("/api/projects/{project_id}/luminaires/{luminaire_id}")
    def remove_luminaire(project_id: str, luminaire_id: str, expected_revision: int) -> dict[str, Any]:
        state = projects.get(project_id)
        remaining = [item for item in state.luminaires if item.luminaire_id != luminaire_id]
        if len(remaining) == len(state.luminaires):
            raise HTTPException(status_code=404, detail="Luminaire is not in this project")
        updated = projects.update(
            project_id,
            ProjectUpdate(expected_revision=expected_revision, luminaires=remaining),
        )
        photometry_assets.remove(project_id, luminaire_id)
        return updated.model_dump(mode="json")

    @app.post("/api/projects/{project_id}/deliverables/{kind}")
    def generate_deliverable(
        project_id: str,
        kind: Literal["report", "dialux-task"],
        expected_revision: int,
    ) -> dict[str, Any]:
        state = projects.get(project_id)
        if state.revision != expected_revision:
            raise RevisionConflictError(
                f"Project revision is {state.revision}, but request expected {expected_revision}"
        )
        if kind == "report":
            target = projects.artifact_path(project_id, ".design-report.md")
            target.write_text(build_design_report(state), encoding="utf-8")
        else:
            target = projects.artifact_path(project_id, ".dialux-task.zip")
            target.write_bytes(build_dialux_task_archive(state, photometry_assets))
        return {
            "kind": kind,
            "filename": target.name,
            "download_url": f"/api/projects/{project_id}/deliverables/{kind}",
        }

    @app.get("/api/projects/{project_id}/deliverables/{kind}")
    def download_deliverable(project_id: str, kind: Literal["report", "dialux-task"]):
        state = projects.get(project_id)
        suffix = ".design-report.md" if kind == "report" else ".dialux-task.zip"
        target = projects.artifact_path(state.project_id, suffix)
        if not target.exists():
            raise HTTPException(status_code=404, detail="请先生成该交付文件")
        media_type = "text/markdown" if kind == "report" else "application/zip"
        return FileResponse(target, media_type=media_type, filename=target.name)

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> dict[str, Any]:
        settings = Settings()
        if not settings.llm_api_key:
            raise HTTPException(status_code=503, detail="未配置 LIGHTING_LLM_API_KEY，聊天功能暂不可用")
        session_id = request.session_id or uuid4().hex
        messages = _chat_history(sessions.get(session_id))
        content = request.message
        if request.project_id:
            project = projects.get(request.project_id)
            content = (
                f"当前 project_id 是 {request.project_id}，revision 是 {project.revision}。"
                f"请先用 get_project 读取项目。\n\n用户问题：{request.message}"
            )
            content = _project_chat_content(request.message, request.project_id, project.revision)
        if "agent" not in agent_holder:
            agent_holder["agent"] = build_agent(settings)
        result = agent_holder["agent"].invoke(
            {"messages": [*messages, {"role": "user", "content": content}]}
        )
        output_messages = list(result["messages"])
        answer = str(output_messages[-1].content)
        sessions.save(
            session_id,
            [*messages, {"role": "user", "content": request.message}, {"role": "assistant", "content": answer}],
            project_id=request.project_id,
        )
        return {
            "session_id": session_id,
            "answer": answer,
            "project": projects.get(request.project_id).model_dump(mode="json") if request.project_id else None,
        }

    @app.post("/api/chat/stream")
    def stream_chat(request: ChatRequest) -> StreamingResponse:
        """Stream visible assistant tokens while retaining the chat session."""

        settings = Settings()
        if not settings.llm_api_key:
            raise HTTPException(status_code=503, detail="未配置 LIGHTING_LLM_API_KEY，聊天功能暂不可用")
        session_id = request.session_id or uuid4().hex
        messages = _chat_history(sessions.get(session_id))
        content = request.message
        if request.project_id:
            project = projects.get(request.project_id)
            content = (
                f"当前 project_id 是 {request.project_id}，revision 是 {project.revision}。"
                f"请先用 get_project 读取项目。\n\n用户问题：{request.message}"
            )
        if "agent" not in agent_holder:
            agent_holder["agent"] = build_agent(settings)
        agent = agent_holder["agent"]
        if request.project_id:
            project = projects.get(request.project_id)
            content = _project_chat_content(request.message, request.project_id, project.revision)
        def events():
            """Relay agent output while emitting progress during slow tool/model calls."""

            answer_parts: list[str] = []
            step_statuses = {step["id"]: "pending" for step in _AGENT_WORKFLOW_STEPS}
            started_tool_calls: set[str] = set()
            active_tool_calls: dict[str, str] = {}
            plan_emitted = False
            yield _event_line({"type": "start", "session_id": session_id})
            output_queue: Queue[tuple[str, Any]] = Queue()

            def run_agent_stream() -> None:
                tool_started_at: dict[str, float] = {}
                retry_count = 0

                def notify_retry(detail: str) -> None:
                    nonlocal retry_count
                    retry_count += 1
                    output_queue.put(("retry", {"attempt": retry_count, "max": settings.llm_max_retries, "detail": detail}))

                set_retry_notifier(notify_retry)
                try:
                    last_context_usage: dict[str, Any] | None = None
                    clarification_emitted = False
                    for chunk in agent.stream(
                        {"messages": [*messages, {"role": "user", "content": content}]},
                        stream_mode="messages",
                        config={"recursion_limit": max(4, settings.agent_max_steps)},
                    ):
                        for tool_call in _tool_calls_from_chunk(chunk, include_debug=request.debug):
                            tool_started_at[tool_call["call_id"]] = time.monotonic()
                            if request.debug:
                                tool_call["started_at"] = datetime.now(UTC).isoformat()
                            output_queue.put(("tool_start", tool_call))
                        tool_result = _tool_result_from_chunk(chunk, include_debug=request.debug)
                        if tool_result:
                            started_at = tool_started_at.pop(tool_result["call_id"], None)
                            if request.debug and started_at is not None:
                                tool_result["duration_ms"] = round(
                                    (time.monotonic() - started_at) * 1000
                                )
                            output_queue.put(("tool_end", tool_result))
                            clarification = _clarification_from_tool_chunk(chunk)
                            if clarification:
                                clarification_emitted = True
                                output_queue.put(("clarification", clarification))
                            if request.project_id:
                                output_queue.put(("project", None))
                        context_usage = _context_usage_from_chunk(chunk, settings.llm_context_window_tokens)
                        if context_usage is not None and context_usage != last_context_usage:
                            last_context_usage = context_usage
                            output_queue.put(("context", context_usage))
                        text = _visible_chat_chunk(chunk)
                        if not text:
                            continue
                        answer_parts.append(text)
                        output_queue.put(("delta", text))

                    answer = "".join(answer_parts)
                    if not answer:
                        raise RuntimeError("智能体未返回可显示的文本")
                    if (
                        request.project_id
                        and not clarification_emitted
                        and _claims_structured_clarification(answer)
                    ):
                        output_queue.put(("clarification", _fallback_clarification(projects.get(request.project_id))))
                    sessions.save(
                        session_id,
                        [
                            *messages,
                            {"role": "user", "content": request.message},
                            {"role": "assistant", "content": answer},
                        ],
                        project_id=request.project_id,
                    )
                    output_queue.put(("done", answer))
                except Exception as error:
                    output_queue.put(("error", _chat_error_detail(error, settings)))
                finally:
                    set_retry_notifier(None)

            Thread(target=run_agent_stream, name=f"lighting-chat-{session_id[:8]}", daemon=True).start()
            heartbeat_seconds = max(1.0, settings.chat_stream_heartbeat_seconds)
            while True:
                try:
                    event_type, value = output_queue.get(timeout=heartbeat_seconds)
                except Empty:
                    yield _event_line({"type": "status", "content": "智能体正在分析项目条件或调用工具…"})
                    continue

                if event_type == "delta":
                    yield _event_line({"type": "delta", "content": value})
                    continue
                if event_type == "retry":
                    yield _event_line({"type": "retry", **value})
                    continue
                if event_type == "context":
                    yield _event_line({"type": "context", "usage": value})
                    continue
                if event_type == "tool_start":
                    if not plan_emitted:
                        plan_emitted = True
                        yield _event_line({"type": "plan", "steps": _agent_plan()})
                    call_id = value["call_id"]
                    if call_id in started_tool_calls:
                        continue
                    started_tool_calls.add(call_id)
                    name = value["name"]
                    active_tool_calls[call_id] = name
                    step_id = _AGENT_TOOL_STEPS.get(name)
                    if step_id and step_statuses[step_id] != "active":
                        step_statuses[step_id] = "active"
                        yield _event_line({"type": "step", "step_id": step_id, "status": "active"})
                    yield _event_line({"type": "tool_start", **value})
                    continue
                if event_type == "tool_end":
                    if not plan_emitted:
                        plan_emitted = True
                        yield _event_line({"type": "plan", "steps": _agent_plan()})
                    call_id = value["call_id"]
                    name = value.get("name") or active_tool_calls.get(call_id)
                    if not name:
                        # A malformed ToolMessage has neither a name nor a
                        # corresponding complete call. Do not invent an
                        # "unknown_tool" event for the browser.
                        continue
                    value = {**value, "name": name}
                    if call_id not in started_tool_calls:
                        started_tool_calls.add(call_id)
                        active_tool_calls[call_id] = name
                        step_id = _AGENT_TOOL_STEPS.get(name)
                        if step_id and step_statuses[step_id] != "active":
                            step_statuses[step_id] = "active"
                            yield _event_line({"type": "step", "step_id": step_id, "status": "active"})
                        yield _event_line({"type": "tool_start", "name": value["name"], "call_id": call_id})
                    yield _event_line({"type": "tool_end", **value})
                    active_tool_calls.pop(call_id, None)
                    step_id = _AGENT_TOOL_STEPS.get(name)
                    if step_id:
                        step_statuses[step_id] = value["status"]
                        yield _event_line({"type": "step", "step_id": step_id, "status": value["status"]})
                    continue
                if event_type == "clarification":
                    yield _event_line({"type": "clarification", **value})
                    continue
                if event_type == "project":
                    # A tool may have advanced the revision long before the
                    # assistant produces its final text. Send the authoritative
                    # snapshot immediately so the workbench and next action do
                    # not retain the old project version.
                    if request.project_id:
                        yield _event_line(
                            {
                                "type": "project",
                                "project": projects.get(request.project_id).model_dump(mode="json"),
                            }
                        )
                    continue
                if event_type == "done":
                    # Tool calls must be paired in the UI. A provider that
                    # terminates after a call chunk but before ToolMessage is
                    # surfaced as failed instead of permanently "running".
                    for call_id, name in active_tool_calls.items():
                        yield _event_line(
                            {"type": "tool_end", "name": name, "call_id": call_id, "status": "failed"}
                        )
                        step_id = _AGENT_TOOL_STEPS.get(name)
                        if step_id and step_statuses[step_id] == "active":
                            step_statuses[step_id] = "failed"
                            yield _event_line({"type": "step", "step_id": step_id, "status": "failed"})
                    if plan_emitted:
                        for step_id, status in step_statuses.items():
                            final_status = "done" if status == "active" else "skipped" if status == "pending" else status
                            if final_status != status:
                                yield _event_line({"type": "step", "step_id": step_id, "status": final_status})
                    event: dict[str, Any] = {"type": "done", "session_id": session_id, "answer": value}
                    if request.project_id:
                        event["project"] = projects.get(request.project_id).model_dump(mode="json")
                    yield _event_line(event)
                    return
                # The worker only emits delta, done or error events.
                yield _event_line({"type": "error", "detail": value})
                return

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson; charset=utf-8",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/chat/{session_id}")
    def get_chat_history(session_id: str) -> dict[str, Any]:
        """Restore a persisted chat transcript for the browser's project session."""

        return {
            "session_id": session_id,
            "messages": _chat_history(sessions.get(session_id)),
        }

    @app.delete("/api/chat/{session_id}", status_code=204)
    def clear_chat(session_id: str) -> None:
        sessions.clear(session_id)

    return app


def _safe_upload_name(filename: str) -> str:
    name = Path(filename).name.strip().replace(" ", "_")
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", name)
    return name[:180] or f"document-{uuid4().hex[:8]}.txt"


def _unique_upload_target(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    return directory / f"{candidate.stem}-{uuid4().hex[:8]}{candidate.suffix}"


app = create_app()
