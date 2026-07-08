"""SQLite registry schema for Stage 2."""

import sqlite3
from collections.abc import Callable

SCHEMA_VERSION = "1"


PROJECTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_name_norm TEXT NOT NULL UNIQUE,
    repo_root TEXT NOT NULL,
    repo_root_norm TEXT NOT NULL UNIQUE,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_status TEXT,
    last_indexed_at TEXT NULL,
    last_git_commit TEXT NULL
);
"""

REGISTRY_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS registry_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    project_name TEXT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NULL,
    created_at TEXT NOT NULL
);
"""

META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    tool_version: str,
    event_id: Callable[[], str],
) -> None:
    """Create registry tables and seed meta rows once."""

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(PROJECTS_TABLE_SQL)
    conn.execute(REGISTRY_EVENTS_TABLE_SQL)
    conn.execute(META_TABLE_SQL)

    existing = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if existing is not None:
        return

    created_at = now()
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("schema_version", SCHEMA_VERSION),
            ("created_at", created_at),
            ("tool_version", tool_version),
        ],
    )
    conn.execute(
        """
        INSERT INTO registry_events (
            event_id,
            project_id,
            project_name,
            event_type,
            message,
            details_json,
            created_at
        )
        VALUES (?, NULL, NULL, ?, ?, NULL, ?)
        """,
        (
            event_id(),
            "schema_initialized",
            "Registry schema initialized.",
            created_at,
        ),
    )
