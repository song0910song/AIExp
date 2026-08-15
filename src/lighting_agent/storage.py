"""Shared SQLite primitives for durable local application state."""

from __future__ import annotations

import os
import sqlite3
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class SQLiteDatabase:
    """Open short-lived SQLite connections with WAL and transactional writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        if self.path.exists() and not self._is_healthy():
            self._recover_corrupt_database()
        connection = self.connect()
        try:
            self._create_schema(connection)
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;

            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK (revision >= 0),
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_revisions (
                project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK (revision >= 0),
                state_json TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (project_id, revision)
            );

            CREATE TABLE IF NOT EXISTS documents (
                source_hash TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                page_count INTEGER,
                indexed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_chunks (
                chunk_id TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL REFERENCES documents(source_hash) ON DELETE CASCADE,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                locator TEXT NOT NULL,
                content TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS evidence_chunks_source_hash_idx
                ON evidence_chunks(source_hash);

            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT,
                messages_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS chat_sessions_expires_at_idx
                ON chat_sessions(expires_at);

            CREATE TABLE IF NOT EXISTS legacy_imports (
                source_key TEXT PRIMARY KEY,
                imported_at TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in connection.execute("PRAGMA table_info(chat_sessions)").fetchall()
        }
        if "project_id" not in columns:
            connection.execute("ALTER TABLE chat_sessions ADD COLUMN project_id TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS chat_sessions_project_id_idx ON chat_sessions(project_id)"
        )

    def _is_healthy(self) -> bool:
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            try:
                result = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False
        return result == ["ok"]

    def _recover_corrupt_database(self) -> None:
        """Logically rebuild readable application records and preserve the damaged file."""

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.stem}.corrupt-{timestamp}{self.path.suffix}")
        temporary = self.path.with_name(f"{self.path.stem}.recovery-{timestamp}{self.path.suffix}")
        source_uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        source.row_factory = sqlite3.Row
        destination = sqlite3.connect(temporary)
        try:
            destination.row_factory = sqlite3.Row
            destination.execute("PRAGMA foreign_keys = ON")
            self._create_schema(destination)
            source_tables = {
                str(row[0])
                for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            for table in (
                "projects",
                "documents",
                "project_revisions",
                "evidence_chunks",
                "chat_sessions",
                "legacy_imports",
            ):
                if table not in source_tables:
                    continue
                destination_columns = [
                    str(row[1])
                    for row in destination.execute(f'PRAGMA table_info("{table}")').fetchall()
                ]
                source_columns = {
                    str(row[1])
                    for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                columns = [column for column in destination_columns if column in source_columns]
                if not columns:
                    continue
                quoted_columns = ", ".join(f'"{column}"' for column in columns)
                rows = source.execute(f'SELECT {quoted_columns} FROM "{table}"').fetchall()
                if rows:
                    placeholders = ", ".join("?" for _ in columns)
                    destination.executemany(
                        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
                        [tuple(row[column] for column in columns) for row in rows],
                    )
            destination.commit()
        except Exception as error:
            destination.rollback()
            raise RuntimeError(f"Cannot recover corrupt SQLite database: {self.path}") from error
        finally:
            destination.close()
            source.close()

        try:
            shutil.copy2(self.path, backup)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(f"{backup}{suffix}"))
                    sidecar.unlink()
            os.replace(temporary, self.path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Cannot replace corrupt SQLite database: {self.path}") from error

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def legacy_import_completed(self, source_key: str) -> bool:
        connection = self.connect()
        try:
            return connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_key = ?", (source_key,)
            ).fetchone() is not None
        finally:
            connection.close()
