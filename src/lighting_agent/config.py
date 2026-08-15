"""Configuration and filesystem locations for the lighting assistant."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIRECTORY = PROJECT_ROOT / "data"
PROJECTS_DIRECTORY = DATA_DIRECTORY / "projects"
# Read-only migration source for deployments created before SQLite evidence storage.
LEGACY_RAG_INDEX_FILE = DATA_DIRECTORY / "rag" / "index.json"
USER_DOCUMENTS_DIRECTORY = PROJECT_ROOT / "src" / "data" / "user_docs"
DATABASE_FILE = DATA_DIRECTORY / "lighting_design.sqlite3"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings, read once so secrets are never printed or persisted."""

    llm_model: str = os.getenv("LIGHTING_LLM_MODEL", "deepseek-v4-flash")
    llm_base_url: str = os.getenv("LIGHTING_LLM_BASE_URL", "https://opencode.ai/zen/go/v1/")
    llm_api_key: str | None = os.getenv("LIGHTING_LLM_API_KEY", "sk-MI6DGswt1YQRw7kGfDv1xHf47dqx2RNUhYIpX5KxD605AsFwr2RDM02KWFJEbluZ")
    llm_temperature: float = float(os.getenv("LIGHTING_LLM_TEMPERATURE", "0.3"))
    llm_timeout_seconds: float = float(os.getenv("LIGHTING_LLM_TIMEOUT_SECONDS", "60"))
    # Codex-style: SDK retries transient model failures (429/5xx/connection) up to 5 times with exponential backoff + jitter.
    llm_max_retries: int = int(os.getenv("LIGHTING_LLM_MAX_RETRIES", "5"))
    llm_context_window_tokens: int = int(os.getenv("LIGHTING_LLM_CONTEXT_WINDOW_TOKENS", "1000000"))
    agent_max_steps: int = int(os.getenv("LIGHTING_AGENT_MAX_STEPS", "50"))
    chat_stream_heartbeat_seconds: float = float(
        os.getenv("LIGHTING_CHAT_STREAM_HEARTBEAT_SECONDS", "5")
    )
    dialux_base_url: str = os.getenv("DIALUX_BASE_URL", "https://luminaires.dialux.com")
    dialux_timeout_seconds: float = float(os.getenv("DIALUX_TIMEOUT_SECONDS", "15"))
    dialux_default_max_results: int = int(os.getenv("DIALUX_DEFAULT_MAX_RESULTS", "5"))
    dialux_candidate_pool_size: int = int(os.getenv("DIALUX_CANDIDATE_POOL_SIZE", "12"))
    dialux_detail_max_workers: int = int(os.getenv("DIALUX_DETAIL_MAX_WORKERS", "4"))
    dialux_search_deadline_seconds: float = float(os.getenv("DIALUX_SEARCH_DEADLINE_SECONDS", "25"))
    dialux_cache_ttl_seconds: float = float(os.getenv("DIALUX_CACHE_TTL_SECONDS", "300"))
    dialux_min_request_interval_seconds: float = float(
        os.getenv("DIALUX_MIN_REQUEST_INTERVAL_SECONDS", "0.05")
    )
    dialux_circuit_failure_threshold: int = int(
        os.getenv("DIALUX_CIRCUIT_FAILURE_THRESHOLD", "4")
    )
    dialux_circuit_cooldown_seconds: float = float(
        os.getenv("DIALUX_CIRCUIT_COOLDOWN_SECONDS", "60")
    )
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
    paddleocr_model: str = os.getenv("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")
    paddleocr_timeout_seconds: float = float(os.getenv("PADDLEOCR_TIMEOUT_SECONDS", "900"))
    paddleocr_poll_interval_seconds: float = float(os.getenv("PADDLEOCR_POLL_INTERVAL_SECONDS", "5"))
    chat_session_ttl_hours: int = int(os.getenv("LIGHTING_CHAT_SESSION_TTL_HOURS", "168"))
    chat_session_max_messages: int = int(os.getenv("LIGHTING_CHAT_SESSION_MAX_MESSAGES", "80"))

    def validate_for_agent(self) -> None:
        if not self.llm_api_key:
            raise RuntimeError(
                "LIGHTING_LLM_API_KEY is required for chat mode. "
                "Offline commands such as init-project and calculate do not require it."
            )


def ensure_data_directories() -> None:
    for directory in (DATA_DIRECTORY, PROJECTS_DIRECTORY, USER_DOCUMENTS_DIRECTORY):
        directory.mkdir(parents=True, exist_ok=True)
