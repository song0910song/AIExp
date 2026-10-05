"""Settings for the four active workbench capabilities."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIRECTORY = PROJECT_ROOT / "data"
DATABASE_FILE = DATA_DIRECTORY / "lighting_design.sqlite3"
PROJECTS_DIRECTORY = DATA_DIRECTORY / "projects"
LEGACY_WORKSPACE_REGISTRY_FILE = DATA_DIRECTORY / "workspace_registry.sqlite3"
USER_DOCUMENTS_DIRECTORY = PROJECT_ROOT / "src" / "data" / "user_docs"

load_dotenv(PROJECT_ROOT / ".env")


REASONING_EFFORT_VALUES = ("none", "low", "medium", "high")
REASONING_EFFORT_METADATA = {
    "none": {"label": "关闭", "description": "不启用额外推理"},
    "low": {"label": "低", "description": "适合简单查询"},
    "medium": {"label": "中", "description": "兼顾响应速度与分析"},
    "high": {"label": "高", "description": "适合复杂问题"},
}


def _optional_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if not value or not value.strip():
        return None
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    llm_model: str | None = os.getenv("LIGHTING_LLM_MODEL")
    llm_vision_model: str | None = os.getenv("LIGHTING_VISION_MODEL")
    llm_base_url: str | None = os.getenv("LIGHTING_LLM_BASE_URL")
    llm_api_key: str | None = os.getenv("LIGHTING_LLM_API_KEY")
    llm_temperature: float = float(os.getenv("LIGHTING_LLM_TEMPERATURE", "0.3"))
    llm_timeout_seconds: float = float(os.getenv("LIGHTING_LLM_TIMEOUT_SECONDS", "60"))
    llm_max_retries: int = int(os.getenv("LIGHTING_LLM_MAX_RETRIES", "3"))
    agent_max_steps: int = int(os.getenv("LIGHTING_AGENT_MAX_STEPS", "20"))
    llm_reasoning_efforts: str = os.getenv(
        "LIGHTING_LLM_REASONING_EFFORTS", ",".join(REASONING_EFFORT_VALUES)
    )
    llm_reasoning_effort_default: str = os.getenv("LIGHTING_LLM_REASONING_EFFORT_DEFAULT", "medium")
    llm_reasoning_effort: str | None = None
    llm_prompt_cache_enabled: bool | None = _optional_bool("LIGHTING_LLM_PROMPT_CACHE_ENABLED")
    llm_prompt_cache_key: str = os.getenv(
        "LIGHTING_LLM_PROMPT_CACHE_KEY", "lighting-design-agent-v1"
    ).strip() or "lighting-design-agent-v1"
    llm_prompt_cache_ttl: str = os.getenv("LIGHTING_LLM_PROMPT_CACHE_TTL", "30m")
    llm_context_window_tokens: int = int(os.getenv("LIGHTING_LLM_CONTEXT_WINDOW_TOKENS", "128000"))
    llm_context_window_estimated: bool = not bool(os.getenv("LIGHTING_LLM_CONTEXT_WINDOW_TOKENS"))
    chat_session_ttl_hours: int = int(os.getenv("LIGHTING_CHAT_SESSION_TTL_HOURS", "168"))
    chat_session_max_messages: int = int(os.getenv("LIGHTING_CHAT_SESSION_MAX_MESSAGES", "80"))

    dialux_base_url: str = os.getenv("DIALUX_BASE_URL", "https://luminaires.dialux.com")
    dialux_timeout_seconds: float = float(os.getenv("DIALUX_TIMEOUT_SECONDS", "15"))
    dialux_candidate_pool_size: int = int(os.getenv("DIALUX_CANDIDATE_POOL_SIZE", "12"))
    dialux_detail_max_workers: int = int(os.getenv("DIALUX_DETAIL_MAX_WORKERS", "4"))
    dialux_search_deadline_seconds: float = float(os.getenv("DIALUX_SEARCH_DEADLINE_SECONDS", "25"))
    dialux_cache_ttl_seconds: float = float(os.getenv("DIALUX_CACHE_TTL_SECONDS", "300"))
    dialux_min_request_interval_seconds: float = float(os.getenv("DIALUX_MIN_REQUEST_INTERVAL_SECONDS", "0.05"))
    dialux_circuit_failure_threshold: int = int(os.getenv("DIALUX_CIRCUIT_FAILURE_THRESHOLD", "4"))
    dialux_circuit_cooldown_seconds: float = float(os.getenv("DIALUX_CIRCUIT_COOLDOWN_SECONDS", "60"))

    rag_backend: str = os.getenv("LIGHTING_RAG_BACKEND", "chroma")
    embedding_model: str = os.getenv("LIGHTING_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    embedding_cache_folder: str = os.getenv(
        "LIGHTING_EMBEDDING_CACHE_FOLDER", str(PROJECT_ROOT / ".model-cache")
    )
    embedding_local_files_only: bool = os.getenv(
        "LIGHTING_EMBEDDING_LOCAL_FILES_ONLY", "true"
    ).casefold() in {"1", "true", "yes", "on"}

    paddleocr_api_url: str = os.getenv(
        "PADDLEOCR_API_URL", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    )
    paddleocr_access_token: str | None = os.getenv("PADDLEOCR_ACCESS_TOKEN")
    paddleocr_model: str = os.getenv("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")
    paddleocr_timeout_seconds: float = float(os.getenv("PADDLEOCR_TIMEOUT_SECONDS", "900"))
    paddleocr_poll_interval_seconds: float = float(os.getenv("PADDLEOCR_POLL_INTERVAL_SECONDS", "5"))

    def supported_reasoning_efforts(self) -> tuple[str, ...]:
        configured = (item.strip().casefold() for item in self.llm_reasoning_efforts.split(","))
        efforts = tuple(dict.fromkeys(item for item in configured if item in REASONING_EFFORT_VALUES))
        return efforts or REASONING_EFFORT_VALUES

    def default_reasoning_effort(self) -> str:
        choice = self.llm_reasoning_effort_default.strip().casefold()
        available = self.supported_reasoning_efforts()
        return choice if choice in available else available[0]

    def with_reasoning_effort(self, effort: str | None) -> "Settings":
        return replace(self, llm_reasoning_effort=effort)

    def prompt_cache_options(self) -> dict[str, str] | None:
        if self.llm_prompt_cache_enabled is False:
            return None
        return {"mode": "implicit", "ttl": self.llm_prompt_cache_ttl}

    def validate_for_agent(self) -> None:
        if not self.llm_model or not self.llm_api_key:
            raise RuntimeError("LIGHTING_LLM_MODEL and LIGHTING_LLM_API_KEY are required for chat")

    def vision_model_name(self) -> str | None:
        return (self.llm_vision_model or self.llm_model) if self.llm_api_key else None


def ensure_data_directories() -> None:
    for directory in (DATA_DIRECTORY, PROJECTS_DIRECTORY, USER_DOCUMENTS_DIRECTORY):
        directory.mkdir(parents=True, exist_ok=True)
