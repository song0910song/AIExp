"""Idempotent maintenance operations for the local lighting-design database."""

from __future__ import annotations

import argparse
import sqlite3
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .config import DATABASE_FILE, Settings
from .project_store import ProjectStore
from .rag import ChromaEvidenceStore, LocalEvidenceStore, StoredChunk


def backfill_audit_from_chroma(database_path: Path) -> int:
    """Copy existing Chroma records into the SQLite audit tables by stable chunk ID."""

    chroma_path = database_path.parent / "chroma" / "chroma.sqlite3"
    if not chroma_path.exists():
        return 0
    connection = sqlite3.connect(chroma_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                MAX(CASE WHEN key = 'chunk_id' THEN string_value END) AS chunk_id,
                MAX(CASE WHEN key = 'source_hash' THEN string_value END) AS source_hash,
                MAX(CASE WHEN key = 'source_name' THEN string_value END) AS source_name,
                MAX(CASE WHEN key = 'source_type' THEN string_value END) AS source_type,
                MAX(CASE WHEN key = 'locator' THEN string_value END) AS locator,
                MAX(CASE WHEN key = 'chroma:document' THEN string_value END) AS content
            FROM embedding_metadata
            GROUP BY id
            """
        ).fetchall()
    finally:
        connection.close()

    now = datetime.now(UTC).isoformat()
    chunks = [
        StoredChunk(
            chunk_id=str(row["chunk_id"]),
            source_hash=str(row["source_hash"]),
            source_name=str(row["source_name"]),
            source_type=str(row["source_type"]),
            locator=str(row["locator"]),
            content=str(row["content"]),
            indexed_at=now,
        )
        for row in rows
        if all(
            isinstance(row[name], str) and row[name]
            for name in ("chunk_id", "source_hash", "source_name", "source_type", "locator", "content")
        )
    ]
    return LocalEvidenceStore(database_path=database_path).upsert_chunks(chunks)


def repair_database(database_path: Path = DATABASE_FILE) -> dict[str, int]:
    """Synchronize evidence and re-evaluate every saved project against its brief."""

    evidence_chunks = backfill_audit_from_chroma(database_path)
    project_store = ProjectStore(database_path=database_path)
    projects = project_store.list()
    changed_candidates = 0
    repaired_projects = 0
    for project in projects:
        _, changed = project_store.revalidate_luminaires(project.project_id)
        changed_candidates += changed
        repaired_projects += int(changed > 0)
    return {
        "evidence_chunks": evidence_chunks,
        "projects": repaired_projects,
        "luminaires": changed_candidates,
    }


def rebuild_chroma_index(database_path: Path = DATABASE_FILE) -> int:
    """Quarantine the current Chroma files and rebuild vectors from SQLite evidence."""

    # Opening the audit store first validates or logically recovers the SQLite source.
    audit_store = LocalEvidenceStore(database_path=database_path)
    chunks = audit_store._load()  # noqa: SLF001 - maintenance rebuild reads the durable evidence source
    chroma_directory = database_path.parent / "chroma"
    if chroma_directory.exists():
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = chroma_directory.with_name(f"{chroma_directory.name}.corrupt-{timestamp}")
        shutil.move(str(chroma_directory), str(backup))

    store = ChromaEvidenceStore(Settings(), database_path=database_path)
    indexed_count = store.vector_store._collection.count()  # noqa: SLF001 - verifies the rebuilt collection
    if indexed_count != len(chunks):
        raise RuntimeError(
            f"Chroma rebuild indexed {indexed_count} chunks, but SQLite contains {len(chunks)} evidence chunks"
        )
    return indexed_count



def main() -> None:
    parser = argparse.ArgumentParser(description="Repair and synchronize the lighting-design database")
    parser.add_argument("--database", type=Path, default=DATABASE_FILE)
    parser.add_argument(
        "--rebuild-chroma",
        action="store_true",
        help="quarantine the current Chroma index and rebuild it from SQLite evidence",
    )
    args = parser.parse_args()
    database_path = args.database.resolve()
    if args.rebuild_chroma:
        print({"chroma_chunks": rebuild_chroma_index(database_path)})
    else:
        print(repair_database(database_path))


if __name__ == "__main__":
    main()
