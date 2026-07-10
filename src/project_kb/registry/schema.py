"""SQLite registry schema and additive migrations."""

import json
import sqlite3
from collections.abc import Callable

SCHEMA_VERSION = "2"


PROJECTS_TABLE_SQL = """
CREATE TABLE projects (
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
    last_git_commit TEXT NULL,
    repo_fingerprint_json TEXT NULL
);
"""

REGISTRY_EVENTS_TABLE_SQL = """
CREATE TABLE registry_events (
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
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

REQUIRED_TABLES = {"projects", "registry_events", "meta"}
V1_PROJECT_COLUMNS = {
    "project_id",
    "project_name",
    "project_name_norm",
    "repo_root",
    "repo_root_norm",
    "storage_path",
    "created_at",
    "updated_at",
    "last_status",
    "last_indexed_at",
    "last_git_commit",
}
V2_PROJECT_COLUMNS = V1_PROJECT_COLUMNS | {"repo_fingerprint_json"}
REGISTRY_EVENT_COLUMNS = {
    "event_id",
    "project_id",
    "project_name",
    "event_type",
    "message",
    "details_json",
    "created_at",
}
META_COLUMNS = {"key", "value"}
REQUIRED_META_KEYS = {"schema_version", "created_at", "tool_version"}


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    tool_version: str,
    event_id: Callable[[], str],
    allow_create: bool = True,
) -> None:
    """Initialize a new registry or migrate a supported existing registry."""

    conn.execute("PRAGMA foreign_keys = ON")
    tables = _table_names(conn)
    if not tables:
        if not allow_create:
            raise sqlite3.DatabaseError("Registry schema is invalid; no registry tables found")
        _create_schema(conn, now=now, tool_version=tool_version, event_id=event_id)
        return

    missing_tables = REQUIRED_TABLES - tables
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise sqlite3.DatabaseError(f"Registry schema is invalid; missing tables: {missing}")

    _require_columns(conn, "registry_events", REGISTRY_EVENT_COLUMNS)
    _require_columns(conn, "meta", META_COLUMNS)
    meta = _require_meta_values(conn)

    version = meta["schema_version"]
    if version == "1":
        _require_columns(conn, "projects", V1_PROJECT_COLUMNS)
        _migrate_v1_to_v2(conn, now=now, event_id=event_id)
    elif version == SCHEMA_VERSION:
        _require_columns(conn, "projects", V2_PROJECT_COLUMNS)
    else:
        raise sqlite3.DatabaseError(f"Unsupported registry schema version: {version}")


def _create_schema(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    tool_version: str,
    event_id: Callable[[], str],
) -> None:
    conn.execute(PROJECTS_TABLE_SQL)
    conn.execute(REGISTRY_EVENTS_TABLE_SQL)
    conn.execute(META_TABLE_SQL)

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


def _migrate_v1_to_v2(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    event_id: Callable[[], str],
) -> None:
    columns = _column_names(conn, "projects")
    if "repo_fingerprint_json" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_fingerprint_json TEXT NULL")

    migrated_at = now()
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (SCHEMA_VERSION,),
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
        VALUES (?, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            event_id(),
            "schema_migrated",
            "Registry schema migrated.",
            json.dumps({"from_version": "1", "to_version": SCHEMA_VERSION}, sort_keys=True),
            migrated_at,
        ),
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows if not str(row["name"]).startswith("sqlite_")}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    missing = required - _column_names(conn, table)
    if missing:
        column_names = ", ".join(sorted(missing))
        raise sqlite3.DatabaseError(
            f"Registry schema is invalid; table {table} is missing columns: {column_names}"
        )


def _require_meta_values(conn: sqlite3.Connection) -> dict[str, str]:
    values = {
        str(row["key"]): str(row["value"])
        for row in conn.execute("SELECT key, value FROM meta").fetchall()
    }
    missing = REQUIRED_META_KEYS - values.keys()
    if missing:
        key_names = ", ".join(sorted(missing))
        raise sqlite3.DatabaseError(f"Registry schema is invalid; missing meta keys: {key_names}")
    return values
