"""Small HTTP workbench for CAD, documents, questions and DIALux luminaires."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Callable, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .agent import build_agent
from .config import (
    DATABASE_FILE, REASONING_EFFORT_METADATA, Settings, USER_DOCUMENTS_DIRECTORY, ensure_data_directories,
)
from .dialux_api import DialuxAPI, DialuxAPIError
from .dialux_protocol import DialuxProtocolError, open_in_dialux
from .document_loader import DocumentLoadError, load_document
from .floor_plan import MAX_DRAWING_BYTES, FloorPlanParseError, parse_floor_plan
from .project_store import ProjectNotFoundError, ProjectStore, RevisionConflictError
from .rag import EvidenceNotFoundError, create_evidence_store
from .schemas import DesignBrief, LuminaireSearchRequest, ProjectState, ProjectUpdate, StrictModel
from .storage import SQLiteDatabase
from .workspace import WorkspaceError, WorkspaceEvidenceStore, WorkspaceProjectStore


class ProjectCreateRequest(StrictModel):
    project_name: str = Field(min_length=1, max_length=160)
    space_type: str | None = Field(default=None, max_length=100)
    workspace_selection_id: str | None = None


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

    def get(self, session_id: str, project_id: str | None) -> list[dict[str, str]]:
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
        return [
            {"role": item["role"], "content": item["content"]}
            for item in records
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ]

    def save(self, session_id: str, project_id: str | None, messages: list[dict[str, str]]) -> None:
        now = datetime.now(UTC)
        payload = json.dumps(messages[-max(1, self.settings.chat_session_max_messages):], ensure_ascii=False)
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


def choose_workspace_directory() -> Path | None:
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
            selected = filedialog.askdirectory(parent=window, title="选择照明项目文件夹", mustexist=True)
        finally:
            window.destroy()
    except Exception as error:
        raise RuntimeError(f"无法打开 Windows 文件夹选择器：{error}") from error
    return Path(selected).resolve() if selected else None


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


def create_app(
    *,
    project_store: ProjectStore | WorkspaceProjectStore | None = None,
    evidence_store: Any | None = None,
    dialux_api: DialuxAPI | None = None,
    directory_picker: Callable[[], Path | None] | None = None,
    user_documents_directory: Path | None = None,
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
    documents_root = user_documents_directory or USER_DOCUMENTS_DIRECTORY
    selections: dict[str, Path] = {}
    selection_lock = Lock()

    def project_root(project_id: str) -> Path:
        directory_for = getattr(projects, "directory_for", None)
        return directory_for(project_id) if callable(directory_for) else projects.directory

    def project_sessions(project_id: str | None) -> ChatSessionStore:
        if project_id and isinstance(projects, WorkspaceProjectStore):
            return ChatSessionStore(projects.database_path_for(project_id), settings)
        return sessions

    app = FastAPI(title="照明设计知识工作台 API", version="0.2.0")
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

    @app.post("/api/workspaces/select-directory")
    def select_workspace_directory() -> dict[str, Any]:
        try:
            directory = picker()
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if directory is None:
            return {"selected": False}
        directory = directory.resolve()
        if not directory.is_dir():
            raise HTTPException(status_code=422, detail="项目文件夹不存在")
        selection_id = uuid4().hex
        with selection_lock:
            selections[selection_id] = directory
        return {"selected": True, "selection_id": selection_id, "directory": str(directory)}

    @app.post("/api/projects", status_code=201)
    def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
        brief = DesignBrief(project_name=request.project_name, space_type=request.space_type)
        if not isinstance(projects, WorkspaceProjectStore):
            return _project_view(projects.create(brief))
        with selection_lock:
            directory = selections.pop(request.workspace_selection_id, None)
        if directory is None:
            raise HTTPException(status_code=422, detail="请先选择项目文件夹")
        try:
            return _project_view(projects.create_workspace(brief, directory))
        except WorkspaceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

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
        except (FloorPlanParseError, ValueError) as error:
            if not existed:
                target.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RevisionConflictError:
            if not existed:
                target.unlink(missing_ok=True)
            raise
        return {"floor_plan": plan.model_dump(mode="json"), "project": _project_view(updated)}

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
        return {"evidence": [item.model_dump(mode="json") for item in results]}

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
        return {**_document_view(item), "content": _merge_chunks([chunk.content for chunk in chunks])}

    @app.get("/api/documents/{source_hash}")
    def get_document(source_hash: str) -> dict[str, Any]:
        return document_content(source_hash, None)

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
        return {"source_name": document.source_name, "sha256": document.sha256, "indexed_chunks": count}

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
                dialux=dialux, project_id=request.project_id,
            )
            response = agent.invoke(
                {"messages": [*history, current]},
                config={"recursion_limit": max(4, settings.agent_max_steps)},
            )
            answer = response["messages"][-1].content
            if not isinstance(answer, str):
                answer = " ".join(str(part.get("text", "")) for part in answer if isinstance(part, dict))
        except Exception as error:
            raise HTTPException(status_code=502, detail="照明问答服务暂不可用，请稍后重试") from error
        session_store.save(session_id, request.project_id, [*history, current, {"role": "assistant", "content": answer}])
        return {
            "session_id": session_id,
            "answer": answer,
            "project": _project_view(projects.get(request.project_id)) if request.project_id else None,
        }

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
