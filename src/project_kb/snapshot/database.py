"""Versioned SQLite snapshot schema, validation, publication, and exact queries."""

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from project_kb.errors import IndexingError, SnapshotQueryError
from project_kb.indexing.extractor import EXTRACTOR_NAME, EXTRACTOR_VERSION
from project_kb.indexing.models import RepoState, ScanFacts, ScanPolicy
from project_kb.indexing.policy import normalize_relative_path, path_key, policy_metadata

SCHEMA_VERSION = 1
SCANNER_VERSION = "stage4-v0-3"
EXPECTED_EXTRACTORS = {EXTRACTOR_NAME: EXTRACTOR_VERSION}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE snapshot_meta (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    scanner_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    extractor_versions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    repo_root_norm TEXT NOT NULL,
    git_head TEXT,
    git_branch TEXT,
    git_status_fingerprint TEXT NOT NULL,
    repo_state_before_json TEXT NOT NULL,
    repo_state_after_json TEXT NOT NULL,
    build_status TEXT NOT NULL CHECK (build_status IN ('BUILDING', 'SEALED')),
    logical_fingerprint TEXT NOT NULL,
    observation_fingerprint TEXT NOT NULL
);
CREATE TABLE files (
    file_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    git_population TEXT NOT NULL,
    file_kind TEXT NOT NULL,
    language TEXT,
    extension TEXT NOT NULL,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    line_count INTEGER,
    encoding TEXT,
    content_hash TEXT,
    analysis_level TEXT NOT NULL,
    classification_reason TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    UNIQUE(snapshot_id, path_key)
);
CREATE TABLE pruned_roots (
    pruned_root_id INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_policy TEXT NOT NULL,
    UNIQUE(snapshot_id, relative_path)
);
CREATE TABLE symbols (
    symbol_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    qualified_name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    symbol_kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_column INTEGER NOT NULL,
    end_column INTEGER,
    parent_symbol_id TEXT REFERENCES symbols(symbol_id),
    signature_text TEXT,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);
CREATE TABLE imports (
    import_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    import_kind TEXT NOT NULL,
    module_text TEXT NOT NULL,
    imported_name TEXT,
    alias TEXT,
    relative_level INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    resolution_status TEXT NOT NULL,
    resolved_file_id TEXT REFERENCES files(file_id),
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);
CREATE TABLE relations (
    relation_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    relation_kind TEXT NOT NULL,
    source_file_id TEXT REFERENCES files(file_id),
    source_symbol_id TEXT REFERENCES symbols(symbol_id),
    target_file_id TEXT REFERENCES files(file_id),
    target_symbol_id TEXT REFERENCES symbols(symbol_id),
    target_text TEXT,
    start_line INTEGER,
    end_line INTEGER,
    start_column INTEGER,
    end_column INTEGER,
    resolution_status TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);
CREATE TABLE parse_diagnostics (
    diagnostic_id INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    diagnostic_kind TEXT NOT NULL,
    message TEXT NOT NULL,
    line INTEGER,
    column INTEGER,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL
);
CREATE TABLE index_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_code TEXT,
    candidate_count INTEGER NOT NULL,
    candidate_bytes INTEGER NOT NULL,
    metadata_only_count INTEGER NOT NULL,
    pruned_root_count INTEGER NOT NULL,
    text_file_count INTEGER NOT NULL,
    parsed_file_count INTEGER NOT NULL,
    parse_failure_count INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL,
    import_count INTEGER NOT NULL,
    relation_count INTEGER NOT NULL,
    enumeration_ms INTEGER NOT NULL,
    classification_ms INTEGER NOT NULL,
    read_hash_ms INTEGER NOT NULL,
    parse_ms INTEGER NOT NULL,
    relations_ms INTEGER NOT NULL,
    database_write_ms INTEGER NOT NULL,
    total_ms INTEGER NOT NULL,
    snapshot_size_bytes INTEGER NOT NULL,
    published_snapshot_id TEXT NOT NULL REFERENCES snapshot_meta(snapshot_id),
    warnings_json TEXT NOT NULL
);
CREATE INDEX files_relative_path_idx ON files(relative_path);
CREATE INDEX symbols_short_name_idx ON symbols(short_name, qualified_name);
CREATE INDEX symbols_file_idx ON symbols(file_id, start_line);
CREATE INDEX imports_file_idx ON imports(file_id, start_line);
CREATE INDEX relations_kind_idx ON relations(relation_kind);
CREATE INDEX diagnostics_file_idx ON parse_diagnostics(file_id, line);
"""

REQUIRED_TABLE_COLUMNS = {
    "snapshot_meta": {
        "snapshot_id",
        "project_id",
        "run_id",
        "schema_version",
        "scanner_version",
        "policy_version",
        "policy_json",
        "extractor_versions_json",
        "created_at",
        "repo_root_norm",
        "git_head",
        "git_branch",
        "git_status_fingerprint",
        "repo_state_before_json",
        "repo_state_after_json",
        "build_status",
        "logical_fingerprint",
        "observation_fingerprint",
    },
    "files": {
        "file_id",
        "snapshot_id",
        "relative_path",
        "path_key",
        "git_population",
        "file_kind",
        "language",
        "extension",
        "size_bytes",
        "mtime_ns",
        "line_count",
        "encoding",
        "content_hash",
        "analysis_level",
        "classification_reason",
        "parse_status",
    },
    "pruned_roots": {
        "pruned_root_id",
        "snapshot_id",
        "relative_path",
        "category",
        "reason",
        "source_policy",
    },
    "symbols": {
        "symbol_id",
        "snapshot_id",
        "file_id",
        "qualified_name",
        "short_name",
        "symbol_kind",
        "start_line",
        "end_line",
        "start_column",
        "end_column",
        "parent_symbol_id",
        "signature_text",
        "extractor_name",
        "extractor_version",
    },
    "imports": {
        "import_id",
        "snapshot_id",
        "file_id",
        "import_kind",
        "module_text",
        "imported_name",
        "alias",
        "relative_level",
        "start_line",
        "end_line",
        "resolution_status",
        "resolved_file_id",
        "extractor_name",
        "extractor_version",
    },
    "relations": {
        "relation_id",
        "snapshot_id",
        "relation_kind",
        "source_file_id",
        "source_symbol_id",
        "target_file_id",
        "target_symbol_id",
        "target_text",
        "start_line",
        "end_line",
        "start_column",
        "end_column",
        "resolution_status",
        "evidence_kind",
        "extractor_name",
        "extractor_version",
    },
    "parse_diagnostics": {
        "diagnostic_id",
        "snapshot_id",
        "file_id",
        "diagnostic_kind",
        "message",
        "line",
        "column",
        "extractor_name",
        "extractor_version",
    },
    "index_runs": {
        "run_id",
        "project_id",
        "started_at",
        "finished_at",
        "status",
        "failure_code",
        "candidate_count",
        "candidate_bytes",
        "metadata_only_count",
        "pruned_root_count",
        "text_file_count",
        "parsed_file_count",
        "parse_failure_count",
        "symbol_count",
        "import_count",
        "relation_count",
        "enumeration_ms",
        "classification_ms",
        "read_hash_ms",
        "parse_ms",
        "relations_ms",
        "database_write_ms",
        "total_ms",
        "snapshot_size_bytes",
        "published_snapshot_id",
        "warnings_json",
    },
}
REQUIRED_INDEXES = {
    "files_relative_path_idx",
    "symbols_short_name_idx",
    "symbols_file_idx",
    "imports_file_idx",
    "relations_kind_idx",
    "diagnostics_file_idx",
}


def write_snapshot(
    path: Path,
    *,
    snapshot_id: str,
    run_id: str,
    project_id: str,
    repo_root_norm: str,
    created_at: str,
    started_at: str,
    finished_at: str,
    before: RepoState,
    after: RepoState,
    facts: ScanFacts,
    policy: ScanPolicy,
    warnings: list[dict[str, Any]],
    total_ms: int,
) -> tuple[str, str, int, int]:
    logical_fingerprint = logical_snapshot_fingerprint(facts, policy)
    observation_fingerprint = snapshot_observation_fingerprint(facts, before)
    write_start = time.perf_counter_ns()
    try:
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                """INSERT INTO snapshot_meta VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    snapshot_id,
                    project_id,
                    run_id,
                    SCHEMA_VERSION,
                    SCANNER_VERSION,
                    policy.policy_version,
                    json.dumps(policy_metadata(policy), sort_keys=True),
                    json.dumps(EXPECTED_EXTRACTORS, sort_keys=True),
                    created_at,
                    repo_root_norm,
                    before.head,
                    before.branch,
                    before.status_fingerprint,
                    json.dumps(before.to_dict(), sort_keys=True),
                    json.dumps(after.to_dict(), sort_keys=True),
                    "BUILDING",
                    logical_fingerprint,
                    observation_fingerprint,
                ),
            )
            conn.executemany(
                """INSERT INTO files VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                [
                    (
                        item.file_id,
                        snapshot_id,
                        item.relative_path,
                        item.path_key,
                        item.git_population,
                        item.file_kind,
                        item.language,
                        item.extension,
                        item.size_bytes,
                        item.mtime_ns,
                        item.line_count,
                        item.encoding,
                        item.content_hash,
                        item.analysis_level,
                        item.classification_reason,
                        item.parse_status,
                    )
                    for item in facts.files
                ],
            )
            conn.executemany(
                """INSERT INTO pruned_roots (
                    snapshot_id, relative_path, category, reason, source_policy
                ) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        snapshot_id,
                        item.relative_path,
                        item.category,
                        item.reason,
                        item.source_policy,
                    )
                    for item in facts.pruned_roots
                ],
            )
            conn.executemany(
                """INSERT INTO symbols VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                [
                    (
                        item.symbol_id,
                        snapshot_id,
                        item.file_id,
                        item.qualified_name,
                        item.short_name,
                        item.symbol_kind,
                        item.start_line,
                        item.end_line,
                        item.start_column,
                        item.end_column,
                        item.parent_symbol_id,
                        item.signature_text,
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                    )
                    for item in facts.symbols
                ],
            )
            conn.executemany(
                """INSERT INTO imports VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                [
                    (
                        item.import_id,
                        snapshot_id,
                        item.file_id,
                        item.import_kind,
                        item.module_text,
                        item.imported_name,
                        item.alias,
                        item.relative_level,
                        item.start_line,
                        item.end_line,
                        item.resolution_status,
                        item.resolved_file_id,
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                    )
                    for item in facts.imports
                ],
            )
            conn.executemany(
                """INSERT INTO relations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                [
                    (
                        item.relation_id,
                        snapshot_id,
                        item.relation_kind,
                        item.source_file_id,
                        item.source_symbol_id,
                        item.target_file_id,
                        item.target_symbol_id,
                        item.target_text,
                        item.start_line,
                        item.end_line,
                        item.start_column,
                        item.end_column,
                        item.resolution_status,
                        item.evidence_kind,
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                    )
                    for item in facts.relations
                ],
            )
            conn.executemany(
                """INSERT INTO parse_diagnostics (
                    snapshot_id, file_id, diagnostic_kind, message, line, column,
                    extractor_name, extractor_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        snapshot_id,
                        item.file_id,
                        item.diagnostic_kind,
                        item.message,
                        item.line,
                        item.column,
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                    )
                    for item in facts.diagnostics
                ],
            )
            database_write_ms = (time.perf_counter_ns() - write_start) // 1_000_000
            conn.execute(
                """INSERT INTO index_runs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )""",
                (
                    run_id,
                    project_id,
                    started_at,
                    finished_at,
                    "BUILDING",
                    None,
                    facts.candidate_count,
                    facts.candidate_bytes,
                    facts.counts()["metadata_only"],
                    len(facts.pruned_roots),
                    facts.counts()["text_files"],
                    facts.counts()["parsed_files"],
                    facts.counts()["parse_failures"],
                    len(facts.symbols),
                    len(facts.imports),
                    len(facts.relations),
                    facts.timings.get("enumeration_ms", 0),
                    facts.timings.get("classification_ms", 0),
                    facts.timings.get("read_hash_ms", 0),
                    facts.timings.get("parse_ms", 0),
                    facts.timings.get("relations_ms", 0),
                    database_write_ms,
                    total_ms,
                    0,
                    snapshot_id,
                    json.dumps(warnings, sort_keys=True),
                ),
            )
            conn.commit()
        size = path.stat().st_size
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """UPDATE index_runs
                   SET snapshot_size_bytes = ?, status = 'BUILD_SUCCEEDED'
                   WHERE run_id = ?""",
                (size, run_id),
            )
            conn.execute(
                "UPDATE snapshot_meta SET build_status = 'SEALED' WHERE run_id = ?",
                (run_id,),
            )
            conn.commit()
        return logical_fingerprint, observation_fingerprint, size, database_write_ms
    except (OSError, sqlite3.Error) as exc:
        raise IndexingError(
            "Structural snapshot database could not be built.",
            details={"error_type": type(exc).__name__},
        ) from exc


def validate_snapshot(
    path: Path,
    *,
    project_id: str | None = None,
    expected_policy_version: str | None = None,
) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise SnapshotQueryError(
                "Snapshot path is missing or redirected.", code="SNAPSHOT_STORAGE_ERROR"
            )
        with contextlib.closing(
            sqlite3.connect(f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            if quick != "ok":
                raise SnapshotQueryError(
                    "Snapshot SQLite integrity check failed.", code="SNAPSHOT_STORAGE_ERROR"
                )
            _validate_schema_manifest(conn)
            foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                raise SnapshotQueryError(
                    "Snapshot foreign-key validation failed.", code="SNAPSHOT_STORAGE_ERROR"
                )
            rows = conn.execute("SELECT * FROM snapshot_meta").fetchall()
            row = rows[0] if len(rows) == 1 else None
            if row is None or row["build_status"] != "SEALED":
                raise SnapshotQueryError(
                    "Snapshot metadata is invalid.", code="SNAPSHOT_STORAGE_ERROR"
                )
            if project_id is not None and row["project_id"] != project_id:
                raise SnapshotQueryError(
                    "Snapshot belongs to another project.", code="SNAPSHOT_STORAGE_ERROR"
                )
            compatibility = _compatibility(
                row,
                expected_policy_version=expected_policy_version or ScanPolicy().policy_version,
            )
            if not all(
                compatibility[key]
                for key in (
                    "schema_compatible",
                    "scanner_compatible",
                    "policy_compatible",
                    "extractor_compatible",
                )
            ):
                raise SnapshotQueryError(
                    "Snapshot semantics are incompatible with the current scanner.",
                    code="SNAPSHOT_REBUILD_REQUIRED",
                    details={"compatibility": compatibility},
                )
            _validate_snapshot_invariants(conn, row)
            result = dict(row)
            result["extractor_versions"] = json.loads(row["extractor_versions_json"])
            result["compatibility"] = compatibility
            return result
    except SnapshotQueryError:
        raise
    except (OSError, sqlite3.Error, KeyError) as exc:
        raise SnapshotQueryError(
            "Snapshot could not be read safely.",
            code="SNAPSHOT_STORAGE_ERROR",
            details={"error_type": type(exc).__name__},
        ) from exc


def _validate_schema_manifest(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing_tables = set(REQUIRED_TABLE_COLUMNS) - tables
    missing_columns: dict[str, list[str]] = {}
    for table, required in REQUIRED_TABLE_COLUMNS.items():
        if table not in tables:
            continue
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = sorted(required - actual)
        if missing:
            missing_columns[table] = missing
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    missing_indexes = sorted(REQUIRED_INDEXES - indexes)
    if missing_tables or missing_columns or missing_indexes:
        raise SnapshotQueryError(
            "Snapshot schema manifest is incomplete.",
            code="SNAPSHOT_STORAGE_ERROR",
            details={
                "missing_tables": sorted(missing_tables),
                "missing_columns": missing_columns,
                "missing_indexes": missing_indexes,
            },
        )


def _compatibility(
    row: sqlite3.Row,
    *,
    expected_policy_version: str,
) -> dict[str, bool]:
    try:
        extractors = json.loads(row["extractor_versions_json"])
    except TypeError, json.JSONDecodeError:
        extractors = None
    return {
        "readable": True,
        "schema_compatible": row["schema_version"] == SCHEMA_VERSION,
        "scanner_compatible": row["scanner_version"] == SCANNER_VERSION,
        "policy_compatible": row["policy_version"] == expected_policy_version,
        "extractor_compatible": extractors == EXPECTED_EXTRACTORS,
        "current": False,
    }


def _validate_snapshot_invariants(conn: sqlite3.Connection, meta: sqlite3.Row) -> None:
    snapshot_id = meta["snapshot_id"]
    publication_runs = conn.execute(
        "SELECT * FROM index_runs WHERE published_snapshot_id = ?",
        (snapshot_id,),
    ).fetchall()
    run = publication_runs[0] if len(publication_runs) == 1 else None
    if (
        run is None
        or run["run_id"] != meta["run_id"]
        or run["project_id"] != meta["project_id"]
        or run["published_snapshot_id"] != snapshot_id
        or run["status"] != "BUILD_SUCCEEDED"
    ):
        raise SnapshotQueryError(
            "Snapshot metadata has no unique compatible index run.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    for table in ("files", "pruned_roots", "symbols", "imports", "relations", "parse_diagnostics"):
        mismatched = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE snapshot_id != ?", (snapshot_id,)
        ).fetchone()[0]
        if mismatched:
            raise SnapshotQueryError(
                "Snapshot contains cross-snapshot structural rows.",
                code="SNAPSHOT_STORAGE_ERROR",
                details={"table": table},
            )
    invalid_hashes = conn.execute(
        """SELECT COUNT(*) FROM files
           WHERE (analysis_level = 'TEXT_STRUCTURAL' AND content_hash IS NULL)
              OR (analysis_level != 'TEXT_STRUCTURAL' AND content_hash IS NOT NULL)"""
    ).fetchone()[0]
    if invalid_hashes:
        raise SnapshotQueryError(
            "Snapshot file hash invariants are invalid.", code="SNAPSHOT_STORAGE_ERROR"
        )
    failed_with_facts = conn.execute(
        """SELECT COUNT(*) FROM files f
           WHERE f.parse_status = 'FAILED' AND (
               EXISTS (SELECT 1 FROM symbols s WHERE s.file_id = f.file_id)
               OR EXISTS (SELECT 1 FROM imports i WHERE i.file_id = f.file_id)
               OR EXISTS (SELECT 1 FROM relations r WHERE r.source_file_id = f.file_id)
           )"""
    ).fetchone()[0]
    if failed_with_facts:
        raise SnapshotQueryError(
            "Failed parses contain stale structural facts.", code="SNAPSHOT_STORAGE_ERROR"
        )


def publish_snapshot(temp_path: Path, current_path: Path) -> bool:
    previous = current_path.exists()
    try:
        if current_path.is_symlink() or current_path.is_junction():
            raise OSError("current snapshot is redirected")
        os.replace(temp_path, current_path)
        return previous
    except OSError as exc:
        raise IndexingError(
            "Validated snapshot could not be published atomically.",
            code="SNAPSHOT_PUBLICATION_FAILED",
            details={"error_type": type(exc).__name__, "previous_snapshot_preserved": previous},
        ) from exc


def logical_snapshot_fingerprint(facts: ScanFacts, policy: ScanPolicy) -> str:
    files = []
    for item in facts.files:
        value = asdict(item)
        value.pop("mtime_ns", None)
        value.pop("git_population", None)
        files.append(value)
    payload = {
        "files": sorted(files, key=lambda item: item["path_key"]),
        "pruned_roots": sorted(
            (asdict(item) for item in facts.pruned_roots), key=lambda item: item["relative_path"]
        ),
        "symbols": sorted(
            (asdict(item) for item in facts.symbols), key=lambda item: item["symbol_id"]
        ),
        "imports": sorted(
            (asdict(item) for item in facts.imports), key=lambda item: item["import_id"]
        ),
        "relations": sorted(
            (asdict(item) for item in facts.relations), key=lambda item: item["relation_id"]
        ),
        "diagnostics": sorted(
            (asdict(item) for item in facts.diagnostics),
            key=lambda item: (item["file_id"], item["line"] or 0),
        ),
        "versions": {
            "schema": SCHEMA_VERSION,
            "scanner": SCANNER_VERSION,
            "policy": policy_metadata(policy),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_observation_fingerprint(facts: ScanFacts, state: RepoState) -> str:
    payload = {
        "repo_state": state.to_dict(),
        "evidence": sorted(
            (asdict(item) for item in facts.evidence.values()),
            key=lambda item: item["relative_path"],
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SnapshotReader:
    def __init__(self, path: Path, *, project_id: str) -> None:
        self.path = path
        self.meta = validate_snapshot(path, project_id=project_id)

    def symbols(self, *, file: str | None = None, name: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if file:
            self._require_indexed_file(file)
            clauses.append("f.path_key = ?")
            values.append(_query_path_key(file))
        if name is not None:
            clauses.append(
                """(substr(s.short_name, 1, length(?)) = ? COLLATE BINARY
                    OR substr(s.qualified_name, 1, length(?)) = ? COLLATE BINARY)"""
            )
            values.extend([name, name, name, name])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._rows(
            f"""SELECT f.relative_path, f.content_hash,
                       s.qualified_name, s.short_name, s.symbol_kind,
                       s.start_line, s.end_line, s.start_column, s.end_column,
                       s.signature_text, s.extractor_name, s.extractor_version
                FROM symbols s JOIN files f ON f.file_id = s.file_id
                {where} ORDER BY f.path_key, s.start_line, s.start_column""",
            values,
        )

    def imports(self, *, file: str | None = None) -> list[dict[str, Any]]:
        if file:
            self._require_indexed_file(file)
        where = "WHERE f.path_key = ?" if file else ""
        values = [_query_path_key(file)] if file else []
        return self._rows(
            f"""SELECT f.relative_path, f.content_hash,
                       i.import_kind, i.module_text, i.imported_name,
                       i.alias, i.relative_level, i.start_line, i.end_line,
                       i.resolution_status, target.relative_path AS resolved_relative_path,
                       i.extractor_name, i.extractor_version
                FROM imports i JOIN files f ON f.file_id = i.file_id
                LEFT JOIN files target ON target.file_id = i.resolved_file_id
                {where} ORDER BY f.path_key, i.start_line, i.import_id""",
            values,
        )

    def inspect_file(self, file: str) -> dict[str, Any]:
        key = _query_path_key(file)
        files = self._rows("SELECT * FROM files WHERE path_key = ?", [key])
        if not files:
            raise SnapshotQueryError(
                "File is not present in the structural snapshot.",
                code="FILE_NOT_INDEXED",
                details={"file": file},
            )
        diagnostics = self._rows(
            """SELECT d.diagnostic_kind, d.message, d.line, d.column
               FROM parse_diagnostics d JOIN files f ON f.file_id = d.file_id
               WHERE f.path_key = ? ORDER BY d.line, d.column""",
            [key],
        )
        return {
            "file": files[0],
            "symbols": self.symbols(file=file),
            "imports": self.imports(file=file),
            "parse_diagnostics": diagnostics,
        }

    def _require_indexed_file(self, file: str) -> None:
        key = _query_path_key(file)
        if not self._rows("SELECT 1 FROM files WHERE path_key = ?", [key]):
            raise SnapshotQueryError(
                "File is not present in the structural snapshot.",
                code="FILE_NOT_INDEXED",
                details={"file": file},
            )

    def _rows(self, sql: str, values: list[str]) -> list[dict[str, Any]]:
        try:
            with contextlib.closing(
                sqlite3.connect(f"{self.path.resolve(strict=True).as_uri()}?mode=ro", uri=True)
            ) as conn:
                conn.row_factory = sqlite3.Row
                snapshot_row = conn.execute("SELECT snapshot_id FROM snapshot_meta").fetchone()
                if snapshot_row is None or snapshot_row["snapshot_id"] != self.meta["snapshot_id"]:
                    raise SnapshotQueryError(
                        "Snapshot changed while the structural query was being prepared.",
                        code="SNAPSHOT_CHANGED_DURING_QUERY",
                    )
                return [dict(row) for row in conn.execute(sql, values).fetchall()]
        except SnapshotQueryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise SnapshotQueryError(
                "Snapshot query failed.",
                code="SNAPSHOT_STORAGE_ERROR",
                details={"error_type": type(exc).__name__},
            ) from exc


def _query_path_key(path: str) -> str:
    try:
        return path_key(normalize_relative_path(path))
    except ValueError as exc:
        raise SnapshotQueryError(
            "Snapshot query requires a safe repository-relative path.",
            code="INVALID_RELATIVE_PATH",
            details={"file": path},
        ) from exc
