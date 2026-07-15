"""SQLite registry schema and additive migrations."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

SCHEMA_VERSION = "3"


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
    repo_fingerprint_json TEXT NULL,
    repo_binding_generation TEXT NOT NULL,
    snapshot_binding_generation TEXT NOT NULL
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
V3_PROJECT_COLUMNS = V2_PROJECT_COLUMNS | {
    "repo_binding_generation",
    "snapshot_binding_generation",
}
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
        _migrate_to_v3(conn, from_version="1", now=now, event_id=event_id)
    elif version == "2":
        _require_columns(conn, "projects", V2_PROJECT_COLUMNS)
        _migrate_to_v3(conn, from_version="2", now=now, event_id=event_id)
    elif version == SCHEMA_VERSION:
        _require_columns(conn, "projects", V3_PROJECT_COLUMNS)
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


def _migrate_to_v3(
    conn: sqlite3.Connection,
    *,
    from_version: str,
    now: Callable[[], str],
    event_id: Callable[[], str],
) -> None:
    columns = _column_names(conn, "projects")
    if "repo_fingerprint_json" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_fingerprint_json TEXT NULL")
    if "repo_binding_generation" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_binding_generation TEXT NULL")
    if "snapshot_binding_generation" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN snapshot_binding_generation TEXT NULL")
    projects = conn.execute(
        """SELECT project_id, project_name, created_at, updated_at,
                  last_status, last_indexed_at,
                  repo_binding_generation, snapshot_binding_generation
           FROM projects"""
    ).fetchall()
    for project in projects:
        repo_generation = project["repo_binding_generation"] or project["project_id"]
        snapshot_generation = project["snapshot_binding_generation"]
        if snapshot_generation is None:
            snapshot_generation = (
                _new_binding_generation(conn)
                if _legacy_history_requires_rebuild(conn, project)
                else repo_generation
            )
        conn.execute(
            """UPDATE projects
               SET repo_binding_generation = ?, snapshot_binding_generation = ?
               WHERE project_id = ?""",
            (repo_generation, snapshot_generation, project["project_id"]),
        )

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
            json.dumps(
                {"from_version": from_version, "to_version": SCHEMA_VERSION},
                sort_keys=True,
            ),
            migrated_at,
        ),
    )


def _legacy_history_requires_rebuild(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
) -> bool:
    """Fail closed when durable pre-v3 history cannot prove snapshot alignment."""

    if project["last_status"] == "RELINKED_REINDEX_REQUIRED":
        return True
    events = conn.execute(
        """SELECT event_type, details_json, created_at
           FROM registry_events
           WHERE project_id = ?
           ORDER BY created_at, event_id""",
        (project["project_id"],),
    ).fetchall()
    if not events or not any(event["event_type"] == "register" for event in events):
        return True

    relink_times: list[datetime] = []
    successful_index_times: list[datetime] = []
    for event in events:
        try:
            created_at = _parse_utc(event["created_at"])
        except TypeError, ValueError:
            return True
        if event["event_type"] == "relink":
            relink_times.append(created_at)
        elif event["event_type"] == "index_outcome":
            try:
                details = json.loads(event["details_json"])
            except TypeError, json.JSONDecodeError:
                return True
            if not isinstance(details, dict) or not isinstance(details.get("status"), str):
                return True
            if details["status"] == "INDEX_SUCCEEDED":
                successful_index_times.append(created_at)

    last_indexed_at = project["last_indexed_at"]
    if last_indexed_at is None:
        return True
    try:
        _parse_utc(last_indexed_at)
    except TypeError, ValueError:
        return True
    if not successful_index_times:
        return True

    if not relink_times:
        return False
    latest_relink = max(relink_times)
    return not any(indexed_at > latest_relink for indexed_at in successful_index_times)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("registry event timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("registry event timestamp must include UTC offset")
    return parsed


def _new_binding_generation(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT lower(hex(randomblob(16)))").fetchone()[0])


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
