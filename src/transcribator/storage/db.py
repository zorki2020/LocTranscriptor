"""Database connection and migrations.

The database file is created with mode 600 before SQLite opens it. WAL mode, foreign
keys on. The schema version lives in `PRAGMA user_version`; migrations run in order.

Search uses the `search_text` column: the utterance text lowercased, with "ё" -> "е".
So "Ёлка" is found by "елка". The original text from `text` is what gets shown.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_MIGRATIONS: list[str] = [
    """
    CREATE TABLE meeting (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        recorded_at TEXT,
        added_at TEXT NOT NULL,
        duration_s REAL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'done', 'error')),
        summary TEXT,
        summary_model TEXT,
        summary_error TEXT,
        language TEXT NOT NULL DEFAULT 'ru',
        source_kind TEXT NOT NULL CHECK (source_kind IN ('path', 'upload')),
        source_ref TEXT NOT NULL,
        error_code TEXT
    );
    CREATE INDEX meeting_by_date ON meeting (recorded_at DESC, id DESC);

    CREATE TABLE speaker (
        meeting_id INTEGER NOT NULL REFERENCES meeting (id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        display_name TEXT,
        PRIMARY KEY (meeting_id, label)
    );

    CREATE TABLE segment (
        id INTEGER PRIMARY KEY,
        meeting_id INTEGER NOT NULL REFERENCES meeting (id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        start_s REAL NOT NULL,
        end_s REAL NOT NULL,
        speaker_label TEXT NOT NULL,
        text TEXT NOT NULL,
        search_text TEXT NOT NULL,
        UNIQUE (meeting_id, idx)
    );

    CREATE VIRTUAL TABLE segment_fts USING fts5 (
        search_text, content = 'segment', content_rowid = 'id', tokenize = 'unicode61'
    );
    CREATE TRIGGER segment_ai AFTER INSERT ON segment BEGIN
        INSERT INTO segment_fts (rowid, search_text) VALUES (new.id, new.search_text);
    END;
    CREATE TRIGGER segment_ad AFTER DELETE ON segment BEGIN
        INSERT INTO segment_fts (segment_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
    END;
    CREATE TRIGGER segment_au AFTER UPDATE ON segment BEGIN
        INSERT INTO segment_fts (segment_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
        INSERT INTO segment_fts (rowid, search_text) VALUES (new.id, new.search_text);
    END;

    CREATE TABLE job (
        id INTEGER PRIMARY KEY,
        meeting_id INTEGER NOT NULL UNIQUE REFERENCES meeting (id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'error')),
        stage TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX job_by_state ON job (state, id);
    """,
    # 2: the recognition process group, to stop the orphan after a worker crash.
    """
    ALTER TABLE job ADD COLUMN child_pgid INTEGER;
    """,
]
SCHEMA_VERSION = len(_MIGRATIONS)


def _create_private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating it and applying migrations if needed."""
    if not path.exists():
        _create_private_file(path)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.chmod(0o600)
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switching a new database to WAL needs an exclusive lock, and when the web UI and
    the worker open it for the first time simultaneously it may return "locked" at once
    without waiting for busy_timeout. So we retry ourselves."""
    for attempt in range(50):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            if attempt == 49:
                raise
            time.sleep(0.1)


def migrate(conn: sqlite3.Connection) -> None:
    """Apply missing migrations. The version is read inside the transaction: the web UI
    and the worker may open a new database at once, and the second must not repeat the first."""
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            for statement in _split(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number:d}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _split(script: str) -> list[str]:
    """Split a script into statements without breaking trigger bodies (BEGIN ... END;)."""
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        current.append(line)
        text = "\n".join(current).strip()
        if text.endswith(";") and sqlite3.complete_statement(text):
            statements.append(text)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements
