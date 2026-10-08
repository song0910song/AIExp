"""Small HTTP workbench for CAD, documents, questions and DIALux luminaires."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .agent import SYSTEM_PROMPT, build_agent, create_vision_model
from .config import (
    REASONING_EFFORT_METADATA, Settings, USER_DOCUMENTS_DIRECTORY, ensure_data_directories,
)
from .dialux_api import DialuxAPI, DialuxAPIError
from .dialux_service import DialuxExecutionService, DialuxRunNotFoundError
from .dialux_protocol import DialuxProtocolError, open_in_dialux
from .document_loader import DocumentLoadError, load_document
from .floor_plan import MAX_DRAWING_BYTES, FloorPlanParseError, parse_floor_plan
from .legacy_migration import migrate_legacy_workspaces
from .project_store import ProjectNotFoundError, ProjectStore, RevisionConflictError
from .rag import EvidenceNotFoundError, create_evidence_store, public_locator
from .schemas import (
    DesignBrief, DialuxRunRequest, LuminaireSearchRequest, ProjectState, ProjectUpdate, StrictModel,
)
from .storage import SQLiteDatabase
from .review_api import install_review_routes

LOGGER = logging.getLogger(__name__)


class ProjectCreateRequest(StrictModel):
    project_name: str = Field(min_length=1, max_length=160)
    space_type: str | None = Field(default=None, max_length=100)


class DeleteDocumentsRequest(StrictModel):
    source_hashes: list[str] = Field(min_length=1, max_length=100)


class ProjectBriefRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    project_name: str = Field(min_length=1, max_length=160)
    space_type: str | None = Field(default=None, max_length=100)


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)
    project_id: str | None = None


class LuminaireWebRequest(LuminaireSearchRequest):
    expected_revision: int = Field(ge=0)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = None
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9]{8,64}$")
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class ChatSessionStore:
    def __init__(self, path: Path, settings: Settings) -> None:
        self.database = SQLiteDatabase(path)
        self.settings = settings

    def get(self, session_id: str, project_id: str | None) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT messages_json, project_id, expires_at FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return []
        if row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="会话不属于当前项目")
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            return []
        records = json.loads(row["messages_json"])
        if records and isinstance(records[0], dict) and "type" in records[0]:
            from langchain_core.messages import messages_from_dict

            records = [
                {"role": "user" if item.type == "human" else "assistant", "content": str(item.content)}
                for item in messages_from_dict(records)
                if item.type in {"human", "ai"}
            ]
        messages = []
        for item in records:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            if not isinstance(item.get("content"), str):
                continue
            message = {"role": item["role"], "content": item["content"]}
            if item["role"] == "assistant":
                message["content"] = _sanitize_assistant_content(message["content"])
            if item["role"] == "assistant" and isinstance(item.get("tool_calls"), list):
                message["tool_calls"] = item["tool_calls"]
            if item["role"] == "assistant" and isinstance(item.get("context_usage"), dict):
                message["context_usage"] = item["context_usage"]
            messages.append(message)
        return messages

    def save(self, session_id: str, project_id: str | None, messages: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        normalized = [
            {**item, "content": _sanitize_assistant_content(item["content"])}
            if item.get("role") == "assistant" and isinstance(item.get("content"), str)
            else item
            for item in messages
        ]
        payload = json.dumps(normalized[-max(1, self.settings.chat_session_max_messages):], ensure_ascii=False)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO chat_sessions
                   (session_id, project_id, messages_json, created_at, updated_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET messages_json = excluded.messages_json,
                   updated_at = excluded.updated_at, expires_at = excluded.expires_at
                   WHERE chat_sessions.project_id IS excluded.project_id""",
                (
                    session_id, project_id, payload, now.isoformat(), now.isoformat(),
                    (now + timedelta(hours=max(1, self.settings.chat_session_ttl_hours))).isoformat(),
                ),
            )

    def clear(self, session_id: str, project_id: str | None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM chat_sessions WHERE session_id = ? AND project_id IS ?",
                (session_id, project_id),
            )


def _project_view(state: ProjectState) -> dict[str, Any]:
    return {
        "project_id": state.project_id,
        "revision": state.revision,
        "brief": {
            "project_name": state.brief.project_name,
            "space_type": state.brief.space_type,
        },
        "floor_plan": state.floor_plan.model_dump(mode="json") if state.floor_plan else None,
        "luminaires": [item.model_dump(mode="json") for item in state.luminaires],
        "rule_set": state.rule_set.model_dump(mode="json") if state.rule_set else None,
        "invalidated_dependencies": state.invalidated_dependencies,
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


def _safe_name(filename: str) -> str:
    name = filename.replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."} or len(name) > 180:
        raise HTTPException(status_code=422, detail="文件名无效")
    return name


def _upload_target(directory: Path, filename: str, content: bytes) -> tuple[Path, bool]:
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(filename)
    for index in range(100):
        target = directory / (filename if not index else f"{name.stem}-{index}{name.suffix}")
        if not target.exists():
            return target, False
        if hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(content).digest():
            return target, True
    raise HTTPException(status_code=409, detail="同名文件过多，请更改文件名")


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read(MAX_DRAWING_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="文件不能为空")
    if len(content) > MAX_DRAWING_BYTES:
        raise HTTPException(status_code=413, detail="文件不能超过 50 MB")
    return content


def _document_view(item: Any) -> dict[str, Any]:
    return {
        "source_hash": item.source_hash,
        "source_name": item.source_name,
        "source_type": item.source_type,
        "page_count": item.page_count,
        "indexed_at": item.indexed_at,
        "indexed_chunks": item.indexed_chunks,
        "project_id": item.project_id,
    }


def _remove_managed_upload(artifact: dict[str, Any] | None, documents_root: Path) -> None:
    if not artifact or not artifact.get("source_path"):
        return
    root = documents_root.resolve()
    source = Path(str(artifact["source_path"])).resolve()
    if source.is_relative_to(root) and source.is_file():
        source.unlink()


def _merge_chunks(chunks: list[str]) -> str:
    if not chunks:
        return ""
    content = chunks[0]
    for chunk in chunks[1:]:
        overlap = next(
            (length for length in range(min(120, len(content), len(chunk)), 0, -1)
             if content.endswith(chunk[:length])),
            0,
        )
        content += chunk[overlap:] if overlap else "\n\n" + chunk
    return content


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def _contains_internal_cad_decision(value: Any) -> bool:
    """Return whether a decoded value contains model-only CAD decisions.

    Candidate IDs, confidence values and evidence are useful to the validator,
    but are implementation details rather than chat content.  Keep this check
    deliberately narrow so ordinary JSON used in a design answer is preserved.
    """
    if isinstance(value, dict):
        keys = set(value)
        if {"candidate_id", "action"}.issubset(keys):
            return True
        return any(_contains_internal_cad_decision(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_internal_cad_decision(item) for item in value)
    return False


def _balanced_json_fragments(content: str) -> list[tuple[int, int, Any]]:
    """Find balanced JSON objects/arrays embedded in otherwise natural text."""
    fragments: list[tuple[int, int, Any]] = []
    for start, opening in enumerate(content):
        if opening not in "[{":
            continue
        closing = "]" if opening == "[" else "}"
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(content)):
            character = content[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == opening:
                depth += 1
            elif character == closing:
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(content[start:index + 1])
                    except (TypeError, ValueError):
                        break
                    fragments.append((start, index + 1, value))
                    break
    return fragments


def _sanitize_assistant_content(content: str) -> str:
    """Hide raw multimodal CAD decisions from the owner-facing chat.

    The model is instructed to explain tool results in natural language, but a
    provider may still echo a tool payload.  Sanitize at the API boundary as a
    last line of defence; the structured decision remains persisted only in the
    spatial model/audit data.
    """
    if not content or not isinstance(content, str):
        return content
    replacement = "图纸识别结果已整理，请查看图纸工作区中的空间范围和待确认事项。"
    fragments = _balanced_json_fragments(content)
    candidates = [fragment for fragment in fragments if _contains_internal_cad_decision(fragment[2])]
    suspicious: list[tuple[int, int, Any]] = []
    for fragment in candidates:
        if suspicious and fragment[0] < suspicious[-1][1]:
            continue
        suspicious.append(fragment)
    if not suspicious:
        return content
    groups: list[tuple[int, int]] = []
    for start, end, _value in suspicious:
        if groups and all(char.isspace() or char == "," for char in content[groups[-1][1]:start]):
            groups[-1] = (groups[-1][0], end)
        else:
            groups.append((start, end))
    output: list[str] = []
    cursor = 0
    for start, end in groups:
        prefix = content[cursor:start]
        if prefix.strip():
            output.append(prefix)
        output.append(replacement)
        cursor = end
    output.append(content[cursor:])
    result = "".join(output)
    # Avoid leaving a large run of punctuation/blank lines around a replaced
    # JSON array, while preserving any natural-language answer around it.
    return result.replace("\n\n\n", "\n\n").strip()


def _tool_result_summary(name: str, content: Any) -> str:
    try:
        result = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        return "调用完成"
    if isinstance(result, dict):
        if name == "search_evidence" and isinstance(result.get("evidence"), list):
            return f"检索到 {len(result['evidence'])} 条资料"
        if name == "analyze_floor_plan":
            if result.get("status") == "vision_analyzed":
                analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
                return f"已完成图面识别，检测到 {analysis.get('room_count', 0)} 个空间候选"
            return "图面视觉识别未完成"
        if name == "search_luminaires" and isinstance(result.get("candidates"), list):
            return f"找到 {len(result['candidates'])} 款灯具"
        if name == "send_luminaire_to_dialux":
            return "已请求本机 DIALux 导入灯具" if result.get("status") == "launched" else "DIALux 灯具发送未完成"
        if name == "prepare_dialux_run":
            return f"DIALux 任务状态：{result.get('status', 'unknown')}"
        if name == "start_dialux_run":
            return f"DIALux 任务已提交：{result.get('status', 'unknown')}"
        if name == "get_dialux_run":
            return f"DIALux 任务状态：{result.get('status', 'unknown')}"
    return "调用完成"


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _context_usage(
    input_tokens: int | None, settings: Settings,
    messages: list[dict[str, str]], tool_outputs: list[str] | None = None,
) -> dict[str, Any]:
    estimated = input_tokens is None or getattr(settings, "llm_context_window_estimated", True)
    if input_tokens is None:
        content = SYSTEM_PROMPT + json.dumps(messages, ensure_ascii=False) + "\n".join(tool_outputs or [])
        try:
            import tiktoken

            input_tokens = len(tiktoken.get_encoding("o200k_base").encode(content, disallowed_special=()))
        except (ImportError, ValueError):
            input_tokens = max(1, len(content) // 3)
    window = max(1, getattr(settings, "llm_context_window_tokens", 128000))
    return {
        "input_tokens": input_tokens,
        "window_tokens": window,
        "percentage": round(input_tokens / window * 100, 2),
        "estimated": estimated,
    }


def create_app(
    *,
    project_store: ProjectStore | None = None,
    evidence_store: Any | None = None,
    dialux_api: DialuxAPI | None = None,
    user_documents_directory: Path | None = None,
    vision_model: Any | None = None,
) -> FastAPI:
    ensure_data_directories()
    projects = project_store or ProjectStore()
    if project_store is None:
        migrated = migrate_legacy_workspaces(projects)
        if migrated:
            LOGGER.info("Imported %s project(s) from legacy workspaces", migrated)
    global_evidence = evidence_store or create_evidence_store()
    evidence = global_evidence
    dialux = dialux_api or DialuxAPI()
    settings = Settings()
    cad_vision_model = (None if vision_model is False else
                        vision_model if vision_model is not None else create_vision_model(settings))
    dialux_executor = DialuxExecutionService(projects, settings=settings, dialux_api=dialux)
    sessions = ChatSessionStore(projects.database_path, settings)
    documents_root = user_documents_directory or USER_DOCUMENTS_DIRECTORY

    def project_root(project_id: str) -> Path:
        return projects.directory

    def project_sessions(project_id: str | None) -> ChatSessionStore:
        return sessions

    app = FastAPI(title="照明设计知识工作台 API", version="0.2.0")
    install_review_routes(app, projects, evidence, project_root, _project_view)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ProjectNotFoundError)
    async def missing_project(_, error: ProjectNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RevisionConflictError)
    async def stale_project(_, error: RevisionConflictError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(DialuxRunNotFoundError)
    async def missing_dialux_run(_, error: DialuxRunNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_configured": bool(settings.llm_api_key and settings.llm_model),
            "llm_model": settings.llm_model,
            "rag_backend": settings.rag_backend,
            "project_count": projects.count(),
            "llm_reasoning_efforts": list(settings.supported_reasoning_efforts()),
            "llm_reasoning_effort_default": settings.default_reasoning_effort(),
            "llm_reasoning_effort_options": [
                {"value": effort, **REASONING_EFFORT_METADATA[effort]}
                for effort in settings.supported_reasoning_efforts()
            ],
            "llm_prompt_cache_enabled": settings.prompt_cache_options() is not None,
        }

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return [_project_view(item) for item in sorted(projects.list(), key=lambda item: item.updated_at, reverse=True)]

    @app.post("/api/projects", status_code=201)
    def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
        brief = DesignBrief(project_name=request.project_name, space_type=request.space_type)
        return _project_view(projects.create(brief))

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return _project_view(projects.get(project_id))

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str) -> None:
        projects.get(project_id)
        evidence.delete_project(project_id)
        projects.delete(project_id)

    @app.put("/api/projects/{project_id}/brief")
    def update_brief(project_id: str, request: ProjectBriefRequest) -> dict[str, Any]:
        state = projects.get(project_id)
        brief = state.brief.model_copy(
            update={"project_name": request.project_name, "space_type": request.space_type}
        )
        return _project_view(
            projects.update(project_id, ProjectUpdate(expected_revision=request.expected_revision, brief=brief))
        )

    @app.post("/api/projects/{project_id}/floor-plan", status_code=201)
    async def import_floor_plan(
        project_id: str, file: Annotated[UploadFile, File()], expected_revision: Annotated[int, Form(ge=0)]
    ) -> dict[str, Any]:
        state = projects.get(project_id)
        if state.revision != expected_revision:
            raise RevisionConflictError(f"Project revision is {state.revision}, expected {expected_revision}")
        filename = _safe_name(file.filename or "plan.dxf")
        if Path(filename).suffix.casefold() not in {".dxf", ".dwg"}:
            raise HTTPException(status_code=415, detail="仅支持 DXF 和 DWG 平面图")
        content = await _read_upload(file)
        root = project_root(project_id)
        target, existed = _upload_target(root / f"{project_id}.plans", filename, content)
        if not existed:
            await run_in_threadpool(target.write_bytes, content)
        try:
            plan = await run_in_threadpool(parse_floor_plan, target, storage_path=target.relative_to(root).as_posix())
            updated = projects.set_floor_plan(project_id, expected_revision, plan, None)
            analysis = None
            if cad_vision_model is not None:
                from .tools import make_tools

                analyzer = next(item for item in make_tools(
                    projects=projects, evidence=evidence, dialux=dialux, project_id=project_id,
                    vision_model=cad_vision_model, dialux_executor=dialux_executor,
                ) if item.name == "analyze_floor_plan")
                analysis = await run_in_threadpool(analyzer.invoke, {})
                updated = projects.get(project_id)
        except (FloorPlanParseError, ValueError) as error:
            if not existed:
                target.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RevisionConflictError:
            if not existed:
                target.unlink(missing_ok=True)
            raise
        return {"floor_plan": updated.floor_plan.model_dump(mode="json"), "analysis": analysis,
                "project": _project_view(updated)}

    @app.put("/api/projects/{project_id}/floor-plan/selection")
    def select_floor_plan_area(project_id: str, expected_revision: int, candidate_index: int) -> dict[str, Any]:
        state = projects.get(project_id)
        plan = state.floor_plan
        if plan is None:
            raise HTTPException(status_code=404, detail="尚未上传 CAD 图纸")
        if candidate_index < 0 or candidate_index >= len(plan.area_candidates):
            raise HTTPException(status_code=422, detail="房间边界候选不存在")
        root = project_root(project_id).resolve()
        target = (root / plan.asset.storage_path).resolve()
        if root not in target.parents or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != plan.asset.sha256:
            raise HTTPException(status_code=409, detail="原始 CAD 文件已变化，请重新上传")
        try:
            return _project_view(projects.set_floor_plan(project_id, expected_revision, plan, candidate_index))
        except (IndexError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/evidence/search")
    def search_evidence(request: EvidenceSearchRequest) -> dict[str, Any]:
        if request.project_id:
            projects.get(request.project_id)
        results = evidence.search(request.query, top_k=request.top_k, project_id=request.project_id)
        return {
            "evidence": [
                {**item.model_dump(mode="json"), "locator": public_locator(item.locator)}
                for item in results
            ]
        }

    @app.get("/api/documents")
    def list_documents() -> list[dict[str, Any]]:
        return [_document_view(item) for item in evidence.list_documents()]

    @app.get("/api/projects/{project_id}/documents")
    def list_project_documents(project_id: str) -> list[dict[str, Any]]:
        projects.get(project_id)
        return [
            _document_view(item)
            for item in evidence.list_documents(project_id=project_id)
            if item.project_id == project_id
        ]

    def document_content(source_hash: str, project_id: str | None) -> dict[str, Any]:
        documents = evidence.list_documents(project_id=project_id)
        item = next(
            (item for item in documents if item.source_hash == source_hash and item.project_id == project_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        chunks = evidence.get_document_chunks(source_hash, project_id=project_id)
        artifact = evidence.get_document_artifact(source_hash, project_id=project_id)
        if artifact:
            return {**_document_view(item), **{k: v for k, v in artifact.items() if k != "source_path"}}
        return {**_document_view(item), "content": _merge_chunks([chunk.content for chunk in chunks]),
                "pages": [], "extraction_complete": False, "review_required": True}

    @app.get("/api/documents/{source_hash}")
    def get_document(source_hash: str) -> dict[str, Any]:
        return document_content(source_hash, None)

    def delete_global_documents(source_hashes: list[str]) -> None:
        unique_hashes = list(dict.fromkeys(source_hashes))
        available = {item.source_hash for item in evidence.list_documents()}
        missing = [source_hash for source_hash in unique_hashes if source_hash not in available]
        if missing:
            raise HTTPException(status_code=404, detail="全局资料不存在: " + ", ".join(missing))
        artifacts = {
            source_hash: evidence.get_document_artifact(source_hash)
            for source_hash in unique_hashes
        }
        for source_hash in unique_hashes:
            try:
                evidence.delete_document(source_hash)
            except EvidenceNotFoundError as error:
                raise HTTPException(status_code=404, detail="全局资料不存在") from error
            _remove_managed_upload(artifacts[source_hash], documents_root)

    @app.delete("/api/documents/{source_hash}", status_code=204)
    def delete_document(source_hash: str) -> None:
        delete_global_documents([source_hash])

    @app.post("/api/documents/delete", status_code=204)
    def delete_documents(request: DeleteDocumentsRequest) -> None:
        delete_global_documents(request.source_hashes)

    @app.get("/api/projects/{project_id}/documents/{source_hash}")
    def get_project_document(project_id: str, source_hash: str) -> dict[str, Any]:
        projects.get(project_id)
        return document_content(source_hash, project_id)

    async def upload_document(file: UploadFile, source_type: str, project_id: str | None) -> dict[str, Any]:
        if project_id:
            projects.get(project_id)
        filename = _safe_name(file.filename or "document")
        if Path(filename).suffix.casefold() not in {".pdf", ".docx", ".md", ".txt"}:
            raise HTTPException(status_code=415, detail="仅支持 PDF、DOCX、MD 和 TXT")
        content = await _read_upload(file)
        directory = documents_root if project_id is None else project_root(project_id) / f"{project_id}.documents"
        target, existed = _upload_target(directory, filename, content)
        if not existed:
            await run_in_threadpool(target.write_bytes, content)
        try:
            document = await run_in_threadpool(load_document, target, allowed_root=directory)
            count = await run_in_threadpool(
                evidence.add_document, document, source_type=source_type, project_id=project_id
            )
        except DocumentLoadError as error:
            if not existed:
                target.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"source_name": document.source_name, "sha256": document.sha256, "indexed_chunks": count,
                "warnings": [f"{p.locator}: {w}" for p in document.pages for w in p.warnings]}

    @app.post("/api/documents", status_code=201)
    async def add_document(
        file: Annotated[UploadFile, File()],
        source_type: Annotated[Literal["standard", "project_document", "user_note"], Form()] = "standard",
    ) -> dict[str, Any]:
        return await upload_document(file, source_type, None)

    @app.post("/api/projects/{project_id}/documents", status_code=201)
    async def add_project_document(
        project_id: str,
        file: Annotated[UploadFile, File()],
        source_type: Annotated[Literal["project_document", "user_note"], Form()] = "project_document",
    ) -> dict[str, Any]:
        return await upload_document(file, source_type, project_id)

    @app.post("/api/projects/{project_id}/luminaires")
    def search_luminaires(project_id: str, request: LuminaireWebRequest) -> dict[str, Any]:
        projects.get(project_id)
        search = LuminaireSearchRequest.model_validate(request.model_dump(exclude={"expected_revision"}))
        try:
            result = dialux.search_with_run(search, project_id=project_id)
        except DialuxAPIError as error:
            raise HTTPException(status_code=502, detail=error.as_dict()) from error
        updated, _, _ = projects.append_luminaires(
            project_id, request.expected_revision, result.candidates, result.search_run
        )
        ids = {item.luminaire_id for item in result.candidates}
        return {
            "candidates": [item.model_dump(mode="json") for item in updated.luminaires if item.luminaire_id in ids],
            "project": _project_view(updated),
            "warnings": result.search_run.warnings,
        }

    @app.get("/api/projects/{project_id}/luminaires/{luminaire_id}")
    def luminaire_detail(project_id: str, luminaire_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        item = next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="灯具不在当前项目")
        return {"candidate": item.model_dump(mode="json"), "untrusted_supplier_data": True}

    @app.post("/api/projects/{project_id}/luminaires/{luminaire_id}/send-to-dialux")
    def send_luminaire_to_dialux(project_id: str, luminaire_id: str) -> dict[str, Any]:
        state = projects.get(project_id)
        item = next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="灯具不在当前项目")
        try:
            link = dialux.resolve_send_to_dialux_url(item.detail_url)
            open_in_dialux(link)
        except DialuxAPIError as error:
            raise HTTPException(status_code=502, detail=error.as_dict()) from error
        except DialuxProtocolError as error:
            raise HTTPException(status_code=422, detail=error.as_dict()) from error
        return {"status": "launched", "luminaire_id": luminaire_id}

    @app.post("/api/projects/{project_id}/dialux/light-files", status_code=201)
    async def upload_photometry(
        project_id: str,
        file: Annotated[UploadFile, File()],
        expected_revision: Annotated[int, Form(ge=0)],
    ) -> dict[str, Any]:
        state = projects.get(project_id)
        if state.revision != expected_revision:
            raise RevisionConflictError(f"Project revision is {state.revision}, expected {expected_revision}")
        filename = _safe_name(file.filename or "photometry.ies")
        if Path(filename).suffix.casefold() not in {".ies", ".ldt", ".uld"}:
            raise HTTPException(status_code=415, detail="仅支持 IES、LDT 和 ULD 光度文件")
        content = await _read_upload(file)
        directory = project_root(project_id) / f"{project_id}.photometry"
        target, existed = _upload_target(directory, filename, content)
        if not existed:
            await run_in_threadpool(target.write_bytes, content)
        return {
            "path": target.relative_to(projects.directory).as_posix(),
            "source_name": target.name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "project_revision": state.revision,
        }

    @app.post("/api/projects/{project_id}/dialux/readiness")
    def dialux_readiness(project_id: str, request: DialuxRunRequest) -> dict[str, Any]:
        projects.get(project_id)
        readiness, _, layout = dialux_executor.readiness(project_id, request)
        return {"readiness": readiness.model_dump(mode="json"),
                "layout": [item.model_dump(mode="json") for item in layout]}

    @app.post("/api/projects/{project_id}/dialux/runs")
    def prepare_dialux_run(project_id: str, request: DialuxRunRequest) -> dict[str, Any]:
        projects.get(project_id)
        record = dialux_executor.prepare(project_id, request)
        return record.model_dump(mode="json")

    @app.get("/api/projects/{project_id}/dialux/runs")
    def list_dialux_runs(project_id: str) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in dialux_executor.list(project_id)]

    @app.get("/api/projects/{project_id}/dialux/runs/{run_id}")
    def get_dialux_run(project_id: str, run_id: str) -> dict[str, Any]:
        return dialux_executor.get(project_id, run_id).model_dump(mode="json")

    @app.post("/api/projects/{project_id}/dialux/runs/{run_id}/confirm")
    def confirm_dialux_run(project_id: str, run_id: str) -> dict[str, Any]:
        return dialux_executor.confirm(project_id, run_id).model_dump(mode="json")

    @app.post("/api/projects/{project_id}/dialux/runs/{run_id}/start")
    def start_dialux_run(project_id: str, run_id: str) -> dict[str, Any]:
        return dialux_executor.start(project_id, run_id).model_dump(mode="json")

    @app.post("/api/projects/{project_id}/dialux/runs/{run_id}/pause")
    def pause_dialux_run(project_id: str, run_id: str) -> dict[str, Any]:
        return dialux_executor.pause(project_id, run_id).model_dump(mode="json")

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> dict[str, Any]:
        if request.project_id:
            projects.get(request.project_id)
        if not settings.llm_api_key or not settings.llm_model:
            raise HTTPException(status_code=503, detail="未配置照明问答模型")
        if request.reasoning_effort and request.reasoning_effort not in settings.supported_reasoning_efforts():
            raise HTTPException(status_code=422, detail="当前模型不支持所选思考强度")
        session_id = request.session_id or uuid4().hex
        session_store = project_sessions(request.project_id)
        history = session_store.get(session_id, request.project_id)
        current = {"role": "user", "content": request.message}
        try:
            agent = build_agent(
                settings.with_reasoning_effort(request.reasoning_effort),
                projects=projects, evidence=evidence,
                dialux=dialux, dialux_executor=dialux_executor, project_id=request.project_id,
            )
            response = agent.invoke(
                {"messages": [{"role": item["role"], "content": item["content"]} for item in [*history, current]]},
                config={"recursion_limit": max(4, settings.agent_max_steps)},
            )
            final = response["messages"][-1]
            answer = _sanitize_assistant_content(_message_text(final.content))
            tokens = (getattr(final, "usage_metadata", None) or {}).get("input_tokens")
            usage = _context_usage(tokens, settings, [*history, current])
        except Exception as error:
            raise HTTPException(status_code=502, detail="照明问答服务暂不可用，请稍后重试") from error
        session_store.save(session_id, request.project_id, [
            *history, current, {"role": "assistant", "content": answer, "context_usage": usage},
        ])
        return {
            "session_id": session_id,
            "answer": answer,
            "context_usage": usage,
            "project": _project_view(projects.get(request.project_id)) if request.project_id else None,
        }

    @app.post("/api/chat/stream")
    def stream_chat(request: ChatRequest) -> StreamingResponse:
        if request.project_id:
            projects.get(request.project_id)
        if not settings.llm_api_key or not settings.llm_model:
            raise HTTPException(status_code=503, detail="未配置照明问答模型")
        if request.reasoning_effort and request.reasoning_effort not in settings.supported_reasoning_efforts():
            raise HTTPException(status_code=422, detail="当前模型不支持所选思考强度")
        session_id = request.session_id or uuid4().hex
        session_store = project_sessions(request.project_id)
        history = session_store.get(session_id, request.project_id)
        current = {"role": "user", "content": request.message}
        try:
            agent = build_agent(
                settings.with_reasoning_effort(request.reasoning_effort),
                projects=projects, evidence=evidence,
                dialux=dialux, dialux_executor=dialux_executor, project_id=request.project_id,
            )
        except Exception as error:
            LOGGER.exception("Failed to initialize lighting agent")
            raise HTTPException(status_code=502, detail="照明问答服务暂不可用，请稍后重试") from error

        def events():
            from langchain_core.messages import AIMessageChunk

            answer_parts: list[str] = []
            calls: dict[str, dict[str, str]] = {}
            tool_outputs: list[str] = []
            input_tokens: int | None = None
            yield _sse("session", {"session_id": session_id})
            try:
                for mode, chunk in agent.stream(
                    {"messages": [{"role": item["role"], "content": item["content"]} for item in [*history, current]]},
                    stream_mode=["messages", "updates"],
                    config={"recursion_limit": max(4, settings.agent_max_steps)},
                ):
                    if mode == "messages":
                        message, _metadata = chunk
                        if isinstance(message, AIMessageChunk):
                            tokens = (message.usage_metadata or {}).get("input_tokens")
                            if isinstance(tokens, int) and tokens > 0:
                                input_tokens = tokens
                            if not message.tool_call_chunks:
                                delta = _message_text(message.content)
                                if delta:
                                    answer_parts.append(delta)
                    elif mode == "updates":
                        for update in chunk.values():
                            for message in update.get("messages", []):
                                if getattr(message, "type", None) == "ai":
                                    tokens = (getattr(message, "usage_metadata", None) or {}).get("input_tokens")
                                    if isinstance(tokens, int) and tokens > 0:
                                        input_tokens = tokens
                                    for call in getattr(message, "tool_calls", []) or []:
                                        call_id = call.get("id") or uuid4().hex
                                        if call_id not in calls:
                                            args = call.get("args") or {}
                                            entry = {
                                                "id": call_id, "name": call["name"],
                                                "input": json.dumps(args, ensure_ascii=False) if args else "",
                                                "status": "running", "summary": "",
                                            }
                                            calls[call_id] = entry
                                            yield _sse("tool", entry)
                                    if not getattr(message, "tool_calls", None) and not answer_parts:
                                        fallback = _message_text(message.content)
                                        if fallback:
                                            answer_parts.append(fallback)
                                elif getattr(message, "type", None) == "tool":
                                    tool_outputs.append(str(message.content))
                                    call_id = message.tool_call_id
                                    if call_id in calls:
                                        entry = calls[call_id]
                                        entry["status"] = "error" if getattr(message, "status", "success") == "error" else "completed"
                                        entry["summary"] = (
                                            "调用失败" if entry["status"] == "error"
                                            else _tool_result_summary(entry["name"], message.content)
                                        )
                                        yield _sse("tool", entry)
                answer = _sanitize_assistant_content("".join(answer_parts))
                if answer:
                    yield _sse("delta", {"text": answer})
                usage = _context_usage(input_tokens, settings, [*history, current], tool_outputs)
                session_store.save(
                    session_id, request.project_id,
                    [*history, current, {
                        "role": "assistant", "content": answer,
                        "tool_calls": list(calls.values()), "context_usage": usage,
                    }],
                )
                yield _sse("done", {
                    "session_id": session_id,
                    "context_usage": usage,
                    "project": _project_view(projects.get(request.project_id)) if request.project_id else None,
                })
            except Exception:
                LOGGER.exception("Streaming lighting agent failed")
                yield _sse("error", {"message": "照明问答服务暂不可用，请稍后重试"})

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/chat/{session_id}")
    def chat_history(session_id: str, project_id: str | None = None) -> dict[str, Any]:
        if project_id:
            projects.get(project_id)
        return {"session_id": session_id, "messages": project_sessions(project_id).get(session_id, project_id)}

    @app.delete("/api/chat/{session_id}", status_code=204)
    def clear_chat(session_id: str, project_id: str | None = None) -> None:
        project_sessions(project_id).clear(session_id, project_id)

    return app


app = create_app()
