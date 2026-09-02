"""FastAPI adapter for the existing lighting-design domain services.

The web layer deliberately contains no lighting calculations or compliance
rules. It exposes the same ProjectStore, RAG, DIALux and deliverable services
used by the CLI and LangChain tools.
"""

from __future__ import annotations

import json
import hashlib
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Annotated, Any, Callable, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from . import dialux_protocol
from .agent import build_agent, set_retry_notifier
from .calculations import (
    IlluminancePreviewRequest,
    SOLVER_VERSION,
    calculate_lumen_method,
    check_design_rules,
    compute_illuminance_preview,
)
from .calculations.photometry import PhotometryParseError, parse_photometry_file
from .calculations.preview import PreviewGeometryError
from .calculations.layout import analyze_luminaire_layout as run_luminaire_layout_analysis
from .config import DATABASE_FILE, Settings, USER_DOCUMENTS_DIRECTORY, ensure_data_directories
from .deliverables import build_design_report, build_dialux_task_archive, read_dialux_task_package
from .dialux_api import DialuxAPI, DialuxAPIError, validate_luminaire_search
from .dialux_protocol import DialuxProtocolError
from .document_loader import DocumentLoadError, load_document
from .floor_plan import MAX_DRAWING_BYTES, FloorPlanParseError, parse_floor_plan
from .report_parser import LuminaireReportParseError, parse_luminaire_report
from .project_store import ProjectNotFoundError, ProjectStore, RevisionConflictError
from .photometry_assets import PhotometryAssetStore
from .rag import EvidenceNotFoundError, create_evidence_store, format_evidence
from .schemas import (
    CalculationInput,
    DesignBrief,
    LuminaireSearchRequest,
    PhotometryExtractedFile,
    ProjectState,
    ProjectUpdate,
    RuleRequirement,
    SimulationMetrics,
    SimulationRun,
    StrictModel,
)
from .storage import SQLiteDatabase
from .workspace import WorkspaceError, WorkspaceEvidenceStore, WorkspaceProjectStore


class BriefUpdateRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    brief: DesignBrief


class ProjectCreateRequest(DesignBrief):
    """Creation data plus the server-issued token for a native folder choice."""

    workspace_selection_id: str | None = Field(default=None, min_length=8, max_length=64)


class CalculationRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    inputs: CalculationInput | list[CalculationInput]


class RuleCheckRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    requirements: list[RuleRequirement] = Field(min_length=1, max_length=20)
    observations: dict[str, float | int | None]


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)
    project_id: str | None = Field(default=None, min_length=8, max_length=64)


class EvidenceAdoptionRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class LuminaireWebRequest(LuminaireSearchRequest):
    expected_revision: int | None = Field(default=None, ge=0)
    save_to_project: bool = True


class LuminaireSelectionRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    luminaire_ids: list[str] = Field(default_factory=list, max_length=100)
    group_assignments: dict[str, list[str]] = Field(default_factory=dict)


class DialuxResultRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    handoff_id: str = Field(min_length=8, max_length=128)
    input_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metrics: SimulationMetrics
    source_kind: Literal["dialux_pdf", "dialux_csv", "dialux_json", "manual_form"] = "manual_form"
    solver_version: str | None = Field(default=None, max_length=120)
    parser_version: str = Field(default="manual-form-1", max_length=80)


class PhotometryPreviewWebRequest(IlluminancePreviewRequest):
    """Illuminance preview inputs plus the mandatory optimistic-lock revision."""

    expected_revision: int = Field(ge=0)


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


def choose_workspace_directory() -> Path | None:
    """Open the native Windows directory picker for the local desktop user."""

    if sys.platform != "win32":
        raise RuntimeError("项目文件夹选择仅支持运行服务的 Windows 本机")
    try:
        import tkinter as tk
        from tkinter import filedialog

        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        window.update()
        try:
            selected = filedialog.askdirectory(
                parent=window,
                title="选择照明项目文件夹",
                mustexist=True,
            )
        finally:
            window.destroy()
    except Exception as error:
        raise RuntimeError(f"无法打开 Windows 文件夹选择器：{error}") from error
    return Path(selected).resolve() if selected else None


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
        "lighting_groups": ("Lighting regions and groups", "Confirm each region, group, area, mounting-point height, and target illuminance.", "text"),
        "lighting_groups_confirmation": ("Confirm lighting groups", "Confirm the source and value of every group mounting-point height.", "text"),
        "space_type": ("空间类型", "请填写空间用途，例如会议室、教室或走廊。", "text"),
        "area_m2": ("设计面积（m2）", "请填写实际参与照明计算的面积。", "number"),
        "target_illuminance_lx": ("目标照度（lx）", "请填写工作面维持照度目标。", "number"),
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


def _merge_chunk_content(contents: list[str]) -> str:
    """Join chunked text back into a single document, dropping slide overlaps."""

    merged = ""
    for text in contents:
        text = text.strip()
        if not text:
            continue
        if not merged:
            merged = text
            continue
        overlap = 0
        for size in range(min(400, len(text), len(merged)), 39, -1):
            if merged.endswith(text[:size]):
                overlap = size
                break
        merged += text[overlap:]
    return merged


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
        "tools": ["apply_rag_lighting_parameters", "ask_user", "update_project_brief", "update_lighting_groups"],
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
    project_store: ProjectStore | WorkspaceProjectStore | None = None,
    evidence_store: Any | None = None,
    dialux_api: DialuxAPI | None = None,
    directory_picker: Callable[[], Path | None] | None = None,
) -> FastAPI:
    ensure_data_directories()
    projects = project_store or WorkspaceProjectStore()
    global_evidence = evidence_store or create_evidence_store()
    evidence = (
        WorkspaceEvidenceStore(global_evidence, projects)
        if isinstance(projects, WorkspaceProjectStore)
        else global_evidence
    )
    dialux = dialux_api or DialuxAPI()
    settings = Settings()
    sessions = ChatSessionStore(
        DATABASE_FILE if isinstance(projects, WorkspaceProjectStore) else projects.database_path,
        settings,
    )
    picker = directory_picker or choose_workspace_directory
    workspace_selections: dict[str, Path] = {}
    workspace_selection_lock = Lock()
    agent_holder: dict[str, Any] = {}

    # Agent tools are module-level LangChain callables. Bind them to the same
    # workspace-aware stores used by this application instance.
    from . import tools as agent_tools

    agent_tools.configure_runtime_services(projects=projects, evidence=evidence)

    def project_directory(project_id: str) -> Path:
        directory_for = getattr(projects, "directory_for", None)
        return directory_for(project_id) if callable(directory_for) else projects.directory

    def project_sessions(project_id: str | None) -> ChatSessionStore:
        if project_id and isinstance(projects, WorkspaceProjectStore):
            return ChatSessionStore(projects.database_path_for(project_id), settings)
        return sessions

    def project_photometry(project_id: str) -> PhotometryAssetStore:
        return PhotometryAssetStore(project_directory(project_id), dialux)

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

    @app.post("/api/workspaces/select-directory")
    def select_workspace_directory() -> dict[str, Any]:
        """Ask the local Windows host for the directory that will hold one project."""

        try:
            directory = picker()
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if directory is None:
            return {"selected": False}
        try:
            directory = directory.resolve()
            if not directory.is_dir():
                raise WorkspaceError("所选项目文件夹不存在或不是目录")
        except (OSError, WorkspaceError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        selection_id = uuid4().hex
        with workspace_selection_lock:
            workspace_selections[selection_id] = directory
        return {"selected": True, "selection_id": selection_id, "directory": str(directory)}

    @app.post("/api/projects", status_code=201)
    def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
        brief = DesignBrief.model_validate(
            request.model_dump(exclude={"workspace_selection_id"})
        )
        if not isinstance(projects, WorkspaceProjectStore):
            return projects.create(brief).model_dump(mode="json")
        if not request.workspace_selection_id:
            raise HTTPException(status_code=422, detail="请先选择项目文件夹")
        with workspace_selection_lock:
            directory = workspace_selections.get(request.workspace_selection_id)
        if directory is None:
            raise HTTPException(status_code=422, detail="项目文件夹选择已失效，请重新选择")
        try:
            state = projects.create_workspace(brief, directory)
        except WorkspaceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        with workspace_selection_lock:
            workspace_selections.pop(request.workspace_selection_id, None)
        return state.model_dump(mode="json")

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return projects.get(project_id).model_dump(mode="json")

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str) -> None:
        evidence.delete_project(project_id)
        project_photometry(project_id).remove_project(project_id)
        projects.delete(project_id)

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
        if request.project_id:
            projects.get(request.project_id)
        results = evidence.search(request.query, top_k=request.top_k, project_id=request.project_id)
        return {
            "evidence": [item.model_dump(mode="json") for item in results],
            "formatted": format_evidence(results),
        }

    @app.post("/api/projects/{project_id}/evidence")
    def adopt_evidence(project_id: str, request: EvidenceAdoptionRequest) -> dict[str, Any]:
        try:
            adopted = evidence.get_evidence(request.evidence_ids, project_id=project_id)
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

    @app.get("/api/documents")
    def list_global_documents() -> list[dict[str, Any]]:
        """List documents imported into the global knowledge base."""

        return [
            {
                "source_hash": item.source_hash,
                "source_name": item.source_name,
                "source_type": item.source_type,
                "page_count": item.page_count,
                "indexed_at": item.indexed_at,
                "indexed_chunks": item.indexed_chunks,
            }
            for item in evidence.list_documents()
        ]

    @app.delete("/api/documents/{source_hash}", status_code=204)
    def delete_global_document(source_hash: str) -> None:
        """Remove a global document, its RAG chunks and its uploaded file."""

        try:
            document = evidence.delete_document(source_hash)
        except EvidenceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        target = (USER_DOCUMENTS_DIRECTORY / Path(document.source_name).name).resolve()
        root = USER_DOCUMENTS_DIRECTORY.resolve()
        if target.parent == root:
            target.unlink(missing_ok=True)

    @app.get("/api/documents/{source_hash}")
    def get_global_document(source_hash: str) -> dict[str, Any]:
        """Return one global document with its merged full text content."""

        document = next(
            (item for item in evidence.list_documents() if item.source_hash == source_hash),
            None,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="全局资料不存在或已删除")
        chunks = evidence.get_document_chunks(source_hash)
        return {
            "source_hash": document.source_hash,
            "source_name": document.source_name,
            "source_type": document.source_type,
            "page_count": document.page_count,
            "indexed_at": document.indexed_at,
            "indexed_chunks": document.indexed_chunks,
            "content": _merge_chunk_content([chunk.content for chunk in chunks]),
        }

    async def _upload_document(
        file: Annotated[UploadFile, File()],
        source_type: Annotated[Literal["standard", "project_document", "user_note"], Form()] = "project_document",
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if project_id:
            projects.get(project_id)
        if source_type not in {"standard", "project_document", "user_note"}:
            raise HTTPException(status_code=422, detail="项目资料必须从当前项目上传")
        safe_name = _safe_upload_name(file.filename or "document")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in {".pdf", ".docx", ".md", ".txt"}:
            raise HTTPException(status_code=415, detail="仅支持 .pdf、.docx、.md 和 .txt 文件")
        content = await file.read()
        if len(content) > MAX_DRAWING_BYTES:
            raise HTTPException(status_code=413, detail="文件不能超过 50 MB")
        storage_directory = (
            USER_DOCUMENTS_DIRECTORY
            if project_id is None
            else project_directory(project_id) / f"{project_id}.documents"
        )
        storage_directory.mkdir(parents=True, exist_ok=True)
        target = _unique_upload_target(storage_directory, safe_name)
        await run_in_threadpool(target.write_bytes, content)

        def index_upload():
            document = load_document(target, allowed_root=storage_directory)
            return document, evidence.add_document(document, source_type=source_type, project_id=project_id)

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

    @app.post("/api/documents", status_code=201)
    async def add_document(
        file: Annotated[UploadFile, File()],
        source_type: Annotated[Literal["standard", "project_document", "user_note"], Form()] = "standard",
    ) -> dict[str, Any]:
        """Ingest a global knowledge-base document."""

        return await _upload_document(file, source_type, project_id=None)

    @app.post("/api/projects/{project_id}/documents", status_code=201)
    async def add_project_document(
        project_id: str,
        file: Annotated[UploadFile, File()],
        source_type: Annotated[Literal["project_document", "user_note"], Form()] = "project_document",
    ) -> dict[str, Any]:
        """Ingest a document visible only to one project and global evidence."""

        return await _upload_document(file, source_type, project_id=project_id)

    @app.post("/api/projects/{project_id}/calculations")
    def calculate(project_id: str, request: CalculationRequest) -> dict[str, Any]:
        inputs = request.inputs if isinstance(request.inputs, list) else [request.inputs]
        result = [calculate_lumen_method(item) for item in inputs]
        state = projects.get(project_id)
        updated = projects.update(
            project_id,
            ProjectUpdate(
                expected_revision=request.expected_revision,
                calculations=[*state.calculations, *result],
            ),
        )
        return {
            "calculations": [item.model_dump(mode="json") for item in result],
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
        project_root = project_directory(project_id)
        plans_directory = project_root / f"{project_id}.plans"
        target = _unique_upload_target(plans_directory, safe_name)
        await run_in_threadpool(target.write_bytes, content)
        storage_path = str(target.relative_to(project_root).as_posix())
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

    @app.get("/api/projects/{project_id}/layout-analysis")
    def get_layout_analysis(project_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        if state.layout_analysis is None:
            raise HTTPException(status_code=404, detail="项目尚未执行灯具坐标一致性分析")
        return state.layout_analysis.model_dump(mode="json")

    @app.post("/api/projects/{project_id}/layout-analysis", status_code=201)
    async def import_layout_analysis(
        project_id: str,
        expected_revision: Annotated[int, Form(ge=0)],
        report_file: Annotated[UploadFile | None, File()] = None,
        file: Annotated[UploadFile | None, File()] = None,
        cad_file: Annotated[UploadFile | None, File()] = None,
        coordinate_tolerance_m: Annotated[float, Form(gt=0, le=10)] = 0.05,
    ) -> dict[str, Any]:
        """Upload a PDF report and associate it with the project's CAD plan.

        ``file`` is accepted as a compact client-side alias for report_file.
        A missing project plan can be supplied as cad_file in the same request.
        """

        state = projects.get(project_id)
        project_root = project_directory(project_id)
        plans_directory = project_root / f"{project_id}.plans"
        created_targets: list[Path] = []
        working_revision = expected_revision
        if state.floor_plan is None:
            cad_upload = cad_file
            if cad_upload is None and file is not None and Path(file.filename or "").suffix.casefold() in {".dxf", ".dwg"}:
                cad_upload = file
            if cad_upload is None:
                raise HTTPException(status_code=422, detail="请先导入平面图，或在本次请求中提供 cad_file")
            cad_name = _safe_upload_name(cad_upload.filename or "floor-plan.dxf")
            if Path(cad_name).suffix.casefold() not in {".dxf", ".dwg"}:
                raise HTTPException(status_code=415, detail="cad_file 仅支持 .dxf 与 .dwg")
            cad_content = await cad_upload.read()
            if len(cad_content) > MAX_DRAWING_BYTES:
                raise HTTPException(status_code=413, detail="平面图文件不能超过 50 MB")
            cad_target = _unique_upload_target(plans_directory, cad_name)
            await run_in_threadpool(cad_target.write_bytes, cad_content)
            created_targets.append(cad_target)
            try:
                parsed_plan = await run_in_threadpool(
                    parse_floor_plan,
                    cad_target,
                    storage_path=str(cad_target.relative_to(project_root).as_posix()),
                )
                candidate_index = next(
                    (index for index, candidate in enumerate(parsed_plan.area_candidates) if candidate.area_m2 is not None),
                    None,
                )
                state = projects.set_floor_plan(project_id, working_revision, parsed_plan, candidate_index)
                working_revision = state.revision
            except (FloorPlanParseError, IndexError, ValueError) as error:
                await run_in_threadpool(cad_target.unlink, missing_ok=True)
                raise HTTPException(status_code=422, detail=str(error)) from error
        elif state.revision != expected_revision:
            raise RevisionConflictError(
                f"Project revision is {state.revision}, but update expected {expected_revision}"
            )

        report_upload = report_file or file
        if report_upload is None:
            raise HTTPException(status_code=422, detail="请提供 report_file PDF 灯具报告")
        report_name = _safe_upload_name(report_upload.filename or "luminaire-report.pdf")
        if Path(report_name).suffix.casefold() != ".pdf":
            raise HTTPException(status_code=415, detail="report_file 仅支持 PDF")
        report_content = await report_upload.read()
        if len(report_content) > MAX_DRAWING_BYTES:
            raise HTTPException(status_code=413, detail="报告文件不能超过 50 MB")
        report_target = _unique_upload_target(plans_directory, report_name)
        await run_in_threadpool(report_target.write_bytes, report_content)
        created_targets.append(report_target)
        try:
            report = await run_in_threadpool(parse_luminaire_report, report_target)
            analysis = await run_in_threadpool(
                run_luminaire_layout_analysis,
                state.floor_plan,
                report,
                coordinate_tolerance_m=coordinate_tolerance_m,
            )
            updated = projects.set_layout_analysis(project_id, working_revision, analysis)
        except (LuminaireReportParseError, RevisionConflictError, ValueError) as error:
            for target in created_targets:
                await run_in_threadpool(target.unlink, missing_ok=True)
            if isinstance(error, RevisionConflictError):
                raise
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "analysis": updated.layout_analysis.model_dump(mode="json"),
            "project": updated.model_dump(mode="json"),
            "matched_count": sum(
                1 for item in analysis.placements if item.matching_status == "matched"
            ),
            "issue_count": len(analysis.issues),
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

    @app.post("/api/projects/{project_id}/luminaires/{luminaire_id}/send-to-dialux")
    def send_luminaire_to_dialux(project_id: str, luminaire_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        candidate = next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Luminaire is not in this project")
        try:
            dial_url = dialux.resolve_send_to_dialux_url(candidate.detail_url)
        except DialuxAPIError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        try:
            handler = dialux_protocol.open_in_dialux(dial_url)
        except DialuxProtocolError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "status": "launched",
            "luminaire_id": luminaire_id,
            "dialux_protocol_url": dial_url,
            "handler": handler,
        }

    @app.put("/api/projects/{project_id}/selected-luminaires")
    def select_luminaires(project_id: str, request: LuminaireSelectionRequest) -> dict[str, Any]:
        try:
            state = projects.set_selected_luminaires(
                project_id,
                request.expected_revision,
                request.luminaire_ids,
                request.group_assignments,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return state.model_dump(mode="json")

    @app.get("/api/projects/{project_id}/photometry")
    def list_photometry_assets(project_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        return {
            "assets": [
                item.model_dump(mode="json")
                for item in project_photometry(project_id).list_assets(state)
            ],
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
            asset = project_photometry(project_id).download(state, luminaire_id)
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
            (
                item
                for item in project_photometry(project_id).list_assets(state)
                if item.luminaire_id == luminaire_id
            ),
            None,
        )
        if asset is None or asset.status != "downloaded" or asset.zip_file is None:
            raise HTTPException(status_code=404, detail="Photometry ZIP has not been downloaded for this luminaire")
        try:
            target = project_photometry(project_id).read_file(project_id, asset.zip_file)
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
            (
                item
                for item in project_photometry(project_id).list_assets(state)
                if item.luminaire_id == luminaire_id
            ),
            None,
        )
        extracted = next(
            (item for item in (asset.extracted_files if asset is not None else []) if item.relative_path == relative_path),
            None,
        )
        if extracted is None:
            raise HTTPException(status_code=404, detail="未找到该灯具的已验证配光文件")
        try:
            target = project_photometry(project_id).read_file(project_id, extracted.relative_path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="已保存的配光文件不存在") from error
        media_type = "application/octet-stream" if extracted.file_type == "uld" else "text/plain"
        return FileResponse(target, media_type=media_type, filename=target.name)

    # --- Approximate illuminance preview based on parsed photometry files ---

    _preview_artifact_suffix = ".photometry-preview.json"

    def _resolve_photometry_source(
        state: ProjectState, luminaire_id: str
    ) -> tuple[Any, PhotometryExtractedFile]:
        """Return the downloaded ies/ldt asset entry for a final selection."""

        if luminaire_id not in state.selected_luminaire_ids:
            raise HTTPException(
                status_code=422,
                detail="仅最终选定且已下载配光文件的灯具可用于照度预览",
            )
        asset = next(
            (
                item
                for item in project_photometry(state.project_id).list_assets(state)
                if item.luminaire_id == luminaire_id
            ),
            None,
        )
        if asset is None or asset.status != "downloaded":
            raise HTTPException(status_code=422, detail="请先为该灯具下载配光文件")
        ordered = [
            item
            for extension in ("ies", "ldt")
            for item in asset.extracted_files
            if item.file_type == extension
        ]
        if not ordered:
            raise HTTPException(
                status_code=422,
                detail="该灯具暂未提供 IES/LDT 配光文件（ULD 解析暂不支持），无法生成照度预览",
            )
        return asset, ordered[0]

    @app.get("/api/projects/{project_id}/luminaires/{luminaire_id}/photometry/parse")
    def parse_luminaire_photometry(project_id: str, luminaire_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        _, extracted = _resolve_photometry_source(state, luminaire_id)
        try:
            path = project_photometry(project_id).read_file(
                state.project_id,
                extracted.relative_path,
            )
            distribution = parse_photometry_file(path, extracted.file_type)
        except PhotometryParseError as error:
            raise HTTPException(status_code=422, detail=f"配光文件解析失败：{error}") from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="已保存的配光文件不存在") from error
        return {
            "luminaire_id": luminaire_id,
            "source_file": Path(extracted.relative_path).name,
            "summary": distribution.summary(),
        }

    @staticmethod
    def _preview_snapshot_inputs(
        state: ProjectState, request_dict: dict[str, Any], photometry_sha256: str | None
    ) -> dict[str, Any]:
        """Canonical inputs whose change must invalidate the stored preview."""

        return {
            "schema_version": 1,
            "project_revision": state.revision,
            "brief": state.brief.model_dump(mode="json"),
            "selected_luminaire_ids": state.selected_luminaire_ids,
            "photometry_sha256": photometry_sha256,
            "request_fields": request_dict,
        }

    def _preview_snapshot_sha256(snapshot_inputs: dict[str, Any]) -> str:
        canonical = json.dumps(
            snapshot_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @app.post("/api/projects/{project_id}/photometry-preview")
    def create_photometry_preview(project_id: str, request: PhotometryPreviewWebRequest) -> dict[str, Any]:
        state = projects.get(project_id)
        if state.revision != request.expected_revision:
            raise RevisionConflictError(
                f"Project revision is {state.revision}, but request expected {request.expected_revision}"
            )
        candidate = next(
            (item for item in state.luminaires if item.luminaire_id == request.luminaire_id),
            None,
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail="Luminaire is not in this project")

        group = next(
            (
                item
                for item in state.brief.lighting_groups
                if item.group_id == request.lighting_group_id
            ),
            None,
        )
        resolved_request = IlluminancePreviewRequest.model_validate(
            request.model_dump(exclude={"expected_revision"})
        )
        group_maintenance = group.maintenance_factor if group else None
        group_utilization = group.utilization_factor if group else None
        resolved_request = resolved_request.model_copy(
            update={
                "room_length_m": resolved_request.room_length_m or state.brief.length_m,
                "room_width_m": resolved_request.room_width_m or state.brief.width_m,
                "mounting_height_m": resolved_request.mounting_height_m
                or (group.mounting_height_m if group else None),
                "maintenance_factor": resolved_request.maintenance_factor or group_maintenance or 1.0,
                "utilization_factor": resolved_request.utilization_factor or group_utilization,
            }
        )

        store = project_photometry(project_id)
        _, extracted = _resolve_photometry_source(state, request.luminaire_id)
        try:
            path = store.read_file(state.project_id, extracted.relative_path)
            distribution = parse_photometry_file(path, extracted.file_type)
        except PhotometryParseError as error:
            raise HTTPException(status_code=422, detail=f"配光文件解析失败：{error}") from error

        effective_flux = (
            resolved_request.total_flux_lm
            or candidate.luminous_flux_lm
            or distribution.declared_flux_lm
        )
        effective_request = resolved_request.model_copy(update={"total_flux_lm": effective_flux})
        try:
            result = compute_illuminance_preview(distribution, effective_request)
        except PreviewGeometryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        snapshot_inputs = _preview_snapshot_inputs(
            state, effective_request.model_dump(mode="json"), extracted.sha256
        )
        payload = {
            "schema_version": 1,
            "kind": "preview",
            "solver_version": SOLVER_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "input_project_revision": state.revision,
            "input_snapshot_sha256": _preview_snapshot_sha256(snapshot_inputs),
            "snapshot_inputs": snapshot_inputs,
            "luminaire": {
                "luminaire_id": candidate.luminaire_id,
                "article_name": candidate.article_name,
                "source_file": Path(extracted.relative_path).name,
                "file_type": extracted.file_type,
                "photometry_sha256": extracted.sha256,
            },
            "distribution_summary": distribution.summary(),
            "warnings": list(distribution.warnings),
            "result": result.model_dump(mode="json"),
        }
        target = projects.artifact_path(project_id, _preview_artifact_suffix)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
        return {
            "preview": payload,
            "saved": True,
            "download_url": f"/api/projects/{project_id}/photometry-preview",
        }

    @app.get("/api/projects/{project_id}/photometry-preview")
    def get_photometry_preview(project_id: str) -> dict[str, Any]:
        target = projects.artifact_path(project_id, _preview_artifact_suffix)
        if not target.exists():
            raise HTTPException(status_code=404, detail="尚未生成照度预览")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail="预览文件损坏，请重新生成") from error

        state = projects.get(project_id)
        stale_reasons: list[str] = []
        snapshot_inputs = payload.get("snapshot_inputs") if isinstance(payload, dict) else None
        if not isinstance(snapshot_inputs, dict) or snapshot_inputs.get("schema_version") != 1:
            stale_reasons.append("预览格式版本已过期，请重新生成")
        else:
            if snapshot_inputs.get("brief") != state.brief.model_dump(mode="json"):
                stale_reasons.append("设计任务书在生成预览后发生变化")
            if snapshot_inputs.get("selected_luminaire_ids") != state.selected_luminaire_ids:
                stale_reasons.append("最终选定灯具在生成预览后发生变化")
            if snapshot_inputs.get("project_revision") != state.revision:
                stale_reasons.append(
                    f"项目 revision 已从 r{snapshot_inputs.get('project_revision')} 变为 r{state.revision}"
                )
            stored_sha = snapshot_inputs.get("photometry_sha256")
            luminaire_id = (payload.get("luminaire") or {}).get("luminaire_id")
            if stored_sha and luminaire_id:
                luminaire_asset = next(
                    (
                        item
                        for item in project_photometry(project_id).list_assets(state)
                        if item.luminaire_id == luminaire_id
                    ),
                    None,
                )
                live_hashes = {
                    item.sha256 for item in (luminaire_asset.extracted_files if luminaire_asset else [])
                }
                if luminaire_asset is None or stored_sha not in live_hashes:
                    stale_reasons.append("配光文件在生成预览后发生变化")
        return {
            "preview": payload,
            "is_current": not stale_reasons,
            "stale_reasons": stale_reasons,
        }

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
        project_photometry(project_id).remove(project_id, luminaire_id)
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
            target.write_bytes(build_dialux_task_archive(state, project_photometry(project_id)))
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
        session_store = project_sessions(request.project_id)
        messages = _chat_history(session_store.get(session_id))
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
        session_store.save(
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
        session_store = project_sessions(request.project_id)
        messages = _chat_history(session_store.get(session_id))
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
                    session_store.save(
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
    def get_chat_history(session_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Restore a persisted chat transcript for the browser's project session."""

        return {
            "session_id": session_id,
            "messages": _chat_history(project_sessions(project_id).get(session_id)),
        }

    @app.delete("/api/chat/{session_id}", status_code=204)
    def clear_chat(session_id: str, project_id: str | None = None) -> None:
        project_sessions(project_id).clear(session_id)

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
