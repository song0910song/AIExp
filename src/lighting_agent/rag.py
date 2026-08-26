"""Durable evidence retrieval with SQLite and an optional Chroma backend."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .config import DATABASE_FILE, LEGACY_RAG_INDEX_FILE, Settings, ensure_data_directories
from .document_loader import ParsedDocument
from .schemas import Evidence
from .storage import SQLiteDatabase


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{1,}")
CHINESE_PHRASE_PATTERN = re.compile(r"[\u4e00-\u9fff]{3,}")

# Hugging Face tokenizers use interior mutable state and can raise
# ``RuntimeError: Already borrowed`` when one BGE tokenizer is entered from
# concurrent Agent tool threads. Chroma search and indexing share the same
# resource, so serialize only those operations, not the entire Agent run.
_CHROMA_OPERATION_LOCK = RLock()
LOGGER = logging.getLogger(__name__)


class EvidenceNotFoundError(ValueError):
    pass


def tokenize(value: str) -> list[str]:
    tokens: list[str] = []
    for part in TOKEN_PATTERN.findall(value.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.extend(part[index : index + 2] for index in range(max(1, len(part) - 1)))
        else:
            tokens.append(part)
    return tokens


def chroma_keyword_terms(query: str, *, maximum: int = 12) -> list[str]:
    """Return high-signal Chinese n-grams for Chroma ``where_document`` lookup."""

    terms: list[str] = []
    for phrase in CHINESE_PHRASE_PATTERN.findall(query):
        for width in range(min(6, len(phrase)), 2, -1):
            for start in range(len(phrase) - width + 1):
                term = phrase[start : start + width]
                if term not in terms:
                    terms.append(term)
                if len(terms) >= maximum:
                    return terms
    return terms


def chunk_text(text: str, *, size: int = 900, overlap: int = 120) -> list[str]:
    if size <= overlap:
        raise ValueError("size must be greater than overlap")
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    if len(normalized) <= size:
        return [normalized] if normalized else []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        if end < len(normalized):
            boundary = max(normalized.rfind("\n", start, end), normalized.rfind("。", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(normalized[start:end].strip())
        if end == len(normalized):
            break
        start = end - overlap
    return [chunk for chunk in chunks if chunk]


@dataclass(frozen=True, slots=True)
class StoredChunk:
    chunk_id: str
    source_name: str
    source_type: str
    source_hash: str
    locator: str
    content: str
    indexed_at: str
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredDocument:
    source_hash: str
    source_name: str
    source_type: str
    page_count: int | None
    indexed_at: str
    project_id: str | None
    indexed_chunks: int


def stable_chunk_id(source_hash: str, position: int, content: str) -> str:
    """Return the identifier shared by the audit database and Chroma index."""

    return hashlib.sha256(f"{source_hash}:{position}:{content}".encode("utf-8")).hexdigest()


def scoped_source_hash(source_hash: str, project_id: str | None) -> str:
    """Keep identical files in different project scopes as separate records."""

    if project_id is None:
        return source_hash
    return hashlib.sha256(f"project:{project_id}:{source_hash}".encode("utf-8")).hexdigest()


class LocalEvidenceStore:
    """基于 SQLite 的确定性检索，用于离线与测试部署。"""

    def __init__(
        self,
        index_path: Path | None = None,
        *,
        database_path: Path | None = None,
    ) -> None:
        ensure_data_directories()
        self.index_path = index_path
        self.database_path = database_path or (
            DATABASE_FILE if index_path is None else self._database_path_for(index_path)
        )
        self.database = SQLiteDatabase(self.database_path)
        self._import_legacy_index()

    @staticmethod
    def _database_path_for(index_path: Path) -> Path:
        if index_path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            return index_path
        return index_path.with_suffix(".sqlite3")

    def add_document(
        self,
        document: ParsedDocument,
        *,
        source_type: str = "project_document",
        project_id: str | None = None,
    ) -> int:
        contents = chunk_text(document.content)
        now = datetime.now(UTC).isoformat()
        storage_hash = scoped_source_hash(document.sha256, project_id)
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM documents WHERE source_hash = ?", (storage_hash,))
            connection.execute(
                """
                INSERT INTO documents (source_hash, source_name, source_type, project_id, page_count, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (storage_hash, document.source_name, source_type, project_id, document.page_count, now),
            )
            connection.executemany(
                """
                INSERT INTO evidence_chunks
                    (chunk_id, source_hash, source_name, source_type, project_id, locator, content, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        stable_chunk_id(storage_hash, position, content),
                        storage_hash,
                        document.source_name,
                        source_type,
                        project_id,
                        f"chunk {position}",
                        content,
                        now,
                    )
                    for position, content in enumerate(contents, start=1)
                ],
            )
        return len(contents)

    def upsert_chunks(self, chunks: list[StoredChunk]) -> int:
        """Mirror externally indexed chunks into the durable audit tables."""

        if not chunks:
            return 0
        documents = {
            chunk.source_hash: (chunk.source_name, chunk.source_type, chunk.project_id, chunk.indexed_at)
            for chunk in chunks
        }
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO documents (source_hash, source_name, source_type, project_id, page_count, indexed_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(source_hash) DO UPDATE SET
                    source_name = excluded.source_name,
                    source_type = excluded.source_type,
                    project_id = excluded.project_id,
                    indexed_at = excluded.indexed_at
                """,
                [(source_hash, *values) for source_hash, values in documents.items()],
            )
            connection.executemany(
                """
                INSERT INTO evidence_chunks
                    (chunk_id, source_hash, source_name, source_type, project_id, locator, content, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    source_hash = excluded.source_hash,
                    source_name = excluded.source_name,
                    source_type = excluded.source_type,
                    project_id = excluded.project_id,
                    locator = excluded.locator,
                    content = excluded.content,
                    indexed_at = excluded.indexed_at
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.source_hash,
                        chunk.source_name,
                        chunk.source_type,
                        chunk.project_id,
                        chunk.locator,
                        chunk.content,
                        chunk.indexed_at,
                    )
                    for chunk in chunks
                ],
            )
        return len(chunks)

    def search(self, query: str, *, top_k: int = 3, project_id: str | None = None) -> list[Evidence]:
        if not query.strip():
            raise ValueError("query must not be empty")
        query_tokens = set(tokenize(query))
        scored: list[tuple[float, StoredChunk]] = []
        for chunk in self._load(project_id=project_id):
            content_tokens = tokenize(chunk.content)
            if not content_tokens:
                continue
            overlap = sum(1 for token in content_tokens if token in query_tokens)
            if overlap:
                score = overlap / (len(query_tokens) + len(set(content_tokens)) ** 0.5)
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._evidence(chunk, score) for score, chunk in scored[:top_k]]

    def get_evidence(self, evidence_ids: list[str], *, project_id: str | None = None) -> list[Evidence]:
        if not evidence_ids:
            raise ValueError("at least one evidence_id is required")
        unique_ids = list(dict.fromkeys(evidence_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT chunk_id, source_name, source_type, source_hash, project_id, locator, content, indexed_at
                FROM evidence_chunks WHERE chunk_id IN ({placeholders})
                  AND (project_id IS NULL OR project_id = ?)
                """,
                [*unique_ids, project_id] if project_id is not None else [*unique_ids, None],
            ).fetchall()
        finally:
            connection.close()
        chunks = {
            str(row["chunk_id"]): StoredChunk(
                chunk_id=str(row["chunk_id"]),
                source_name=str(row["source_name"]),
                source_type=str(row["source_type"]),
                source_hash=str(row["source_hash"]),
                project_id=str(row["project_id"]) if row["project_id"] is not None else None,
                locator=str(row["locator"]),
                content=str(row["content"]),
                indexed_at=str(row["indexed_at"]),
            )
            for row in rows
        }
        missing = [evidence_id for evidence_id in unique_ids if evidence_id not in chunks]
        if missing:
            raise EvidenceNotFoundError(f"Evidence was not found: {', '.join(missing)}")
        return [self._evidence(chunks[evidence_id]) for evidence_id in unique_ids]

    def get_document_chunks(self, source_hash: str, *, project_id: str | None = None) -> list[StoredChunk]:
        """Return one document's chunks in index order for full-content preview."""

        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT chunk_id, source_name, source_type, source_hash, project_id, locator, content, indexed_at
                FROM evidence_chunks
                WHERE source_hash = ? AND (project_id IS NULL OR project_id = ?)
                ORDER BY rowid ASC
                """,
                (source_hash, project_id) if project_id is not None else (source_hash, None),
            ).fetchall()
        finally:
            connection.close()
        return [
            StoredChunk(
                chunk_id=str(row["chunk_id"]),
                source_name=str(row["source_name"]),
                source_type=str(row["source_type"]),
                source_hash=str(row["source_hash"]),
                project_id=str(row["project_id"]) if row["project_id"] is not None else None,
                locator=str(row["locator"]),
                content=str(row["content"]),
                indexed_at=str(row["indexed_at"]),
            )
            for row in rows
        ]

    def _load(self, *, project_id: str | None = None) -> list[StoredChunk]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT chunk_id, source_name, source_type, source_hash, project_id, locator, content, indexed_at
                FROM evidence_chunks
                WHERE project_id IS NULL OR project_id = ?
                """
                , (project_id,)
            ).fetchall()
        finally:
            connection.close()
        return [
            StoredChunk(
                chunk_id=str(row["chunk_id"]),
                source_name=str(row["source_name"]),
                source_type=str(row["source_type"]),
                source_hash=str(row["source_hash"]),
                project_id=str(row["project_id"]) if row["project_id"] is not None else None,
                locator=str(row["locator"]),
                content=str(row["content"]),
                indexed_at=str(row["indexed_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _evidence(chunk: StoredChunk, score: float | None = None) -> Evidence:
        return Evidence(
            evidence_id=chunk.chunk_id,
            source_name=chunk.source_name,
            source_type=chunk.source_type,  # type: ignore[arg-type]
            excerpt=chunk.content,
            locator=chunk.locator,
            score=round(min(score, 1.0), 4) if score is not None else None,
        )

    def delete_project(self, project_id: str) -> int:
        """Remove all project-scoped evidence and leave global knowledge intact."""

        with self.database.transaction() as connection:
            result = connection.execute("DELETE FROM documents WHERE project_id = ?", (project_id,))
            return int(result.rowcount)

    def list_documents(self, *, project_id: str | None = None) -> list[StoredDocument]:
        """List indexed documents in one scope, including their chunk counts."""

        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT d.source_hash, d.source_name, d.source_type, d.page_count,
                       d.indexed_at, d.project_id, COUNT(c.chunk_id) AS indexed_chunks
                FROM documents AS d
                LEFT JOIN evidence_chunks AS c ON c.source_hash = d.source_hash
                WHERE d.project_id IS NULL OR d.project_id = ?
                GROUP BY d.source_hash
                ORDER BY d.indexed_at DESC, d.source_name COLLATE NOCASE
                """,
                (project_id,),
            ).fetchall()
        finally:
            connection.close()
        return [
            StoredDocument(
                source_hash=str(row["source_hash"]),
                source_name=str(row["source_name"]),
                source_type=str(row["source_type"]),
                page_count=int(row["page_count"]) if row["page_count"] is not None else None,
                indexed_at=str(row["indexed_at"]),
                project_id=str(row["project_id"]) if row["project_id"] is not None else None,
                indexed_chunks=int(row["indexed_chunks"]),
            )
            for row in rows
        ]

    def delete_document(self, source_hash: str) -> StoredDocument:
        """Delete one indexed document and its cascaded evidence chunks."""

        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT d.source_hash, d.source_name, d.source_type, d.page_count,
                       d.indexed_at, d.project_id, COUNT(c.chunk_id) AS indexed_chunks
                FROM documents AS d
                LEFT JOIN evidence_chunks AS c ON c.source_hash = d.source_hash
                WHERE d.source_hash = ? AND d.project_id IS NULL
                GROUP BY d.source_hash
                """,
                (source_hash,),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(f"Document was not found: {source_hash}")
            document = StoredDocument(
                source_hash=str(row["source_hash"]),
                source_name=str(row["source_name"]),
                source_type=str(row["source_type"]),
                page_count=int(row["page_count"]) if row["page_count"] is not None else None,
                indexed_at=str(row["indexed_at"]),
                project_id=str(row["project_id"]) if row["project_id"] is not None else None,
                indexed_chunks=int(row["indexed_chunks"]),
            )
            connection.execute(
                "DELETE FROM documents WHERE source_hash = ? AND project_id IS NULL",
                (source_hash,),
            )
            return document

    def _import_legacy_index(self) -> None:
        legacy_index_path = self.index_path or LEGACY_RAG_INDEX_FILE
        source_key = f"rag-json:{legacy_index_path.resolve()}"
        if self.database.legacy_import_completed(source_key):
            return
        chunks: list[StoredChunk] = []
        if legacy_index_path.exists():
            try:
                raw = json.loads(legacy_index_path.read_text(encoding="utf-8"))
                chunks = [StoredChunk(**item) for item in raw]
            except (OSError, ValueError, TypeError) as error:
                raise RuntimeError(f"Cannot import legacy evidence index: {legacy_index_path}") from error
        with self.database.transaction() as connection:
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO documents (source_hash, source_name, source_type, page_count, indexed_at)
                    VALUES (?, ?, ?, NULL, ?)
                    ON CONFLICT(source_hash) DO NOTHING
                    """,
                    (chunk.source_hash, chunk.source_name, chunk.source_type, chunk.indexed_at),
                )
                connection.execute(
                    """
                    INSERT INTO evidence_chunks
                        (chunk_id, source_hash, source_name, source_type, locator, content, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO NOTHING
                    """,
                    (
                        chunk.chunk_id,
                        chunk.source_hash,
                        chunk.source_name,
                        chunk.source_type,
                        chunk.locator,
                        chunk.content,
                        chunk.indexed_at,
                    ),
                )
            connection.execute(
                "INSERT INTO legacy_imports (source_key, imported_at) VALUES (?, ?)",
                (source_key, datetime.now(UTC).isoformat()),
            )

class ChromaEvidenceStore:
    """Optional semantic backend using Chroma and BGE embeddings."""

    def __init__(self, settings: Settings | None = None, *, database_path: Path = DATABASE_FILE) -> None:
        self.settings = settings or Settings()
        self.database_path = database_path
        self.audit_store = LocalEvidenceStore(database_path=database_path)
        self.chroma_directory = database_path.parent / "chroma"
        try:
            from langchain_chroma import Chroma
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as error:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Chroma backend requires `uv sync --extra chroma` before setting LIGHTING_RAG_BACKEND=chroma."
            ) from error
        self._document_type = self._import_document_type()
        embeddings = HuggingFaceEmbeddings(
            model_name=self.settings.embedding_model,
            cache_folder=self.settings.embedding_cache_folder,
            model_kwargs={"local_files_only": self.settings.embedding_local_files_only},
        )
        self.embeddings = embeddings
        self.vector_store = Chroma(
            collection_name="lighting_design_kb",
            persist_directory=str(self.chroma_directory),
            embedding_function=embeddings,
        )
        with _CHROMA_OPERATION_LOCK:
            self._bootstrap_or_sync_audit_index_unlocked()

    @staticmethod
    def _import_document_type():
        from langchain_core.documents import Document

        return Document

    def add_document(
        self,
        document: ParsedDocument,
        *,
        source_type: str = "project_document",
        project_id: str | None = None,
    ) -> int:
        with _CHROMA_OPERATION_LOCK:
            return self._add_document_unlocked(document, source_type=source_type, project_id=project_id)

    def _add_document_unlocked(
        self,
        document: ParsedDocument,
        *,
        source_type: str,
        project_id: str | None = None,
    ) -> int:
        storage_hash = scoped_source_hash(document.sha256, project_id)
        records = [(position, content) for position, content in enumerate(chunk_text(document.content), start=1)]
        documents = [
            self._document_type(
                page_content=content,
                metadata={
                    "chunk_id": self._chunk_id(storage_hash, position, content),
                    "source_name": document.source_name,
                    "source_type": source_type,
                    "source_hash": storage_hash,
                    "project_id": project_id or "__global__",
                    "locator": f"chunk {position}",
                },
            )
            for position, content in records
        ]
        if documents:
            existing = self.vector_store.get(where={"source_hash": storage_hash})
            existing_ids = {str(item) for item in existing.get("ids", [])}
            ids = [str(item.metadata["chunk_id"]) for item in documents]
            # Compute and write the replacement vectors before removing stale
            # IDs. This prevents an interrupted re-index from leaving a
            # source document absent from the Chroma collection.
            vectors = self.embeddings.embed_documents([item.page_content for item in documents])
            self.vector_store._collection.upsert(  # noqa: SLF001 - required for atomic upsert with vectors
                ids=ids,
                documents=[item.page_content for item in documents],
                metadatas=[item.metadata for item in documents],
                embeddings=vectors,
            )
            stale_ids = existing_ids.difference(ids)
            if stale_ids:
                self.vector_store.delete(ids=list(stale_ids))
            audited_count = self.audit_store.add_document(document, source_type=source_type, project_id=project_id)
            if audited_count != len(documents):
                raise RuntimeError("Chroma and SQLite produced different chunk counts")
        return len(documents)

    @staticmethod
    def _scope_matches(metadata: dict, project_id: str | None) -> bool:
        stored_project = metadata.get("project_id")
        if project_id is None:
            return stored_project in (None, "", "__global__")
        return stored_project in (None, "", "__global__", project_id)

    def search(self, query: str, *, top_k: int = 3, project_id: str | None = None) -> list[Evidence]:
        with _CHROMA_OPERATION_LOCK:
            return self._search_unlocked(query, top_k=top_k, project_id=project_id)

    def _search_unlocked(self, query: str, *, top_k: int, project_id: str | None = None) -> list[Evidence]:
        if not query.strip():
            raise ValueError("query must not be empty")
        # Vector search remains the primary retrieval path. Chroma's document
        # filter complements it for exact Chinese room/type terms embedded in
        # long OCR table chunks, which can otherwise be missed semantically.
        candidates: dict[str, tuple[str, dict, float]] = {}
        candidate_k = top_k if project_id is None else max(top_k * 5, 20)
        for document, score in self.vector_store.similarity_search_with_relevance_scores(query, k=candidate_k):
            metadata = document.metadata if isinstance(document.metadata, dict) else {}
            if not self._scope_matches(metadata, project_id):
                continue
            identifier = str(metadata.get("chunk_id") or document.page_content)
            candidates[identifier] = (document.page_content, metadata, float(score))

        for term in chroma_keyword_terms(query):
            result = self.vector_store.get(
                where_document={"$contains": term},
                include=["documents", "metadatas"],
            )
            for content, metadata in zip(
                result.get("documents", []), result.get("metadatas", []), strict=False
            ):
                if not content or not isinstance(metadata, dict):
                    continue
                if not self._scope_matches(metadata, project_id):
                    continue
                identifier = str(metadata.get("chunk_id") or content)
                existing = candidates.get(identifier)
                # An exact room/type phrase is stronger evidence than a
                # broad semantic neighbour from an OCR table.
                keyword_score = min(1.0, 0.95 + len(term) / 1000)
                candidates[identifier] = (
                    str(content),
                    metadata,
                    max(existing[2], keyword_score) if existing else keyword_score,
                )

        ranked = sorted(candidates.values(), key=lambda item: item[2], reverse=True)
        return [self._evidence(content, metadata, score) for content, metadata, score in ranked[:top_k]]

    def get_evidence(self, evidence_ids: list[str], *, project_id: str | None = None) -> list[Evidence]:
        with _CHROMA_OPERATION_LOCK:
            return self._get_evidence_unlocked(evidence_ids, project_id=project_id)

    def _get_evidence_unlocked(self, evidence_ids: list[str], *, project_id: str | None = None) -> list[Evidence]:
        if not evidence_ids:
            raise ValueError("at least one evidence_id is required")
        unique_ids = list(dict.fromkeys(evidence_ids))
        result = self.vector_store.get(ids=unique_ids, include=["documents", "metadatas"])
        documents = result.get("documents", [])
        metadata_rows = result.get("metadatas", [])
        resolved = {
            str(metadata.get("chunk_id", evidence_id)): self._evidence(str(content), metadata)
            for evidence_id, content, metadata in zip(result.get("ids", []), documents, metadata_rows, strict=False)
            if isinstance(metadata, dict) and self._scope_matches(metadata, project_id)
        }
        missing = [evidence_id for evidence_id in unique_ids if evidence_id not in resolved]
        if missing:
            raise EvidenceNotFoundError(f"Evidence was not found: {', '.join(missing)}")
        return [resolved[evidence_id] for evidence_id in unique_ids]

    @staticmethod
    def _chunk_id(source_hash: str, position: int, content: str) -> str:
        return stable_chunk_id(source_hash, position, content)

    def _sync_audit_index(self) -> None:
        with _CHROMA_OPERATION_LOCK:
            self._sync_audit_index_unlocked()

    def _bootstrap_or_sync_audit_index_unlocked(self) -> None:
        """Use SQLite as the durable source when an empty Chroma index is recreated."""

        try:
            vector_count = self.vector_store._collection.count()  # noqa: SLF001 - validates Chroma storage at startup
        except Exception as error:
            raise RuntimeError(
                "Chroma persistent index cannot be read. Run `uv run python -m lighting_agent.maintenance --rebuild-chroma` "
                "after the service stops; SQLite evidence remains available through the local backend."
            ) from error

        if vector_count:
            self._sync_audit_index_unlocked()
            return

        chunks = self.audit_store._load()  # noqa: SLF001 - the audit store is the recovery source
        if not chunks:
            return

        vectors = self.embeddings.embed_documents([chunk.content for chunk in chunks])
        self.vector_store._collection.upsert(  # noqa: SLF001 - bulk rebuild with stable IDs
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.content for chunk in chunks],
            metadatas=[
                {
                    "chunk_id": chunk.chunk_id,
                    "source_hash": chunk.source_hash,
                    "source_name": chunk.source_name,
                    "source_type": chunk.source_type,
                    "project_id": chunk.project_id or "__global__",
                    "locator": chunk.locator,
                }
                for chunk in chunks
            ],
            embeddings=vectors,
        )
    def _sync_audit_index_unlocked(self) -> None:
        """Backfill Chroma records into SQLite so retrieved evidence remains auditable."""

        result = self.vector_store.get(include=["documents", "metadatas"])
        now = datetime.now(UTC).isoformat()
        chunks: list[StoredChunk] = []
        for content, metadata in zip(
            result.get("documents", []), result.get("metadatas", []), strict=False
        ):
            if not content or not isinstance(metadata, dict):
                continue
            source_hash = metadata.get("source_hash")
            source_name = metadata.get("source_name")
            source_type = metadata.get("source_type")
            project_id = metadata.get("project_id")
            if project_id == "__global__":
                project_id = None
            locator = metadata.get("locator")
            chunk_id = metadata.get("chunk_id")
            if not all(isinstance(value, str) and value for value in (source_hash, source_name, source_type, locator, chunk_id)):
                continue
            chunks.append(
                StoredChunk(
                    chunk_id=chunk_id,
                    source_hash=source_hash,
                    source_name=source_name,
                    source_type=source_type,
                    project_id=project_id if isinstance(project_id, str) and project_id else None,
                    locator=locator,
                    content=str(content),
                    indexed_at=now,
                )
            )
        self.audit_store.upsert_chunks(chunks)

    def delete_project(self, project_id: str) -> int:
        """Delete project vectors and their durable audit rows."""

        with _CHROMA_OPERATION_LOCK:
            result = self.vector_store.get(where={"project_id": project_id}, include=["metadatas"])
            ids = [str(item) for item in result.get("ids", [])]
            if ids:
                self.vector_store.delete(ids=ids)
            return self.audit_store.delete_project(project_id)

    def list_documents(self, *, project_id: str | None = None) -> list[StoredDocument]:
        return self.audit_store.list_documents(project_id=project_id)

    def get_document_chunks(self, source_hash: str, *, project_id: str | None = None) -> list[StoredChunk]:
        return self.audit_store.get_document_chunks(source_hash, project_id=project_id)

    def delete_document(self, source_hash: str) -> StoredDocument:
        with _CHROMA_OPERATION_LOCK:
            document = self.audit_store.delete_document(source_hash)
            result = self.vector_store.get(where={"source_hash": source_hash}, include=["metadatas"])
            ids = [str(item) for item in result.get("ids", [])]
            if ids:
                self.vector_store.delete(ids=ids)
            return document

    @staticmethod
    def _evidence(content: str, metadata: dict, score: float | None = None) -> Evidence:
        return Evidence(
            evidence_id=str(metadata.get("chunk_id") or uuid4().hex),
            source_name=str(metadata.get("source_name", "unknown")),
            source_type=str(metadata.get("source_type", "project_document")),  # type: ignore[arg-type]
            excerpt=content,
            locator=str(metadata.get("locator", "")) or None,
            score=round(max(0.0, min(float(score), 1.0)), 4) if score is not None else None,
        )


def create_evidence_store(settings: Settings | None = None) -> LocalEvidenceStore | ChromaEvidenceStore:
    settings = settings or Settings()
    if settings.rag_backend.casefold() == "chroma":
        try:
            return ChromaEvidenceStore(settings)
        except Exception:
            LOGGER.exception("Chroma is unavailable; using the durable SQLite evidence backend")
            return LocalEvidenceStore()
    if settings.rag_backend.casefold() != "local":
        raise ValueError("LIGHTING_RAG_BACKEND must be local or chroma")
    return LocalEvidenceStore()


def format_evidence(evidence: list[Evidence]) -> str:
    if not evidence:
        return "未检索到可引用的资料。不得据此编造规范结论。"
    return "\n\n".join(
        f"来源：{item.source_name}（{item.locator or '未标注位置'}）\n原文：{item.excerpt}"
        for item in evidence
    )
