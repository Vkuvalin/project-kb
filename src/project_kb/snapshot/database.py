"""Versioned SQLite snapshot schema, validation, publication, and exact queries."""

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from project_kb.errors import IndexingError, SnapshotQueryError
from project_kb.indexing.extractor import EXTRACTOR_NAME, EXTRACTOR_VERSION, stable_id
from project_kb.indexing.identity import (
    OCCURRENCE_CONTRACT_VERSION,
    file_occurrence_id,
    logical_symbol_key,
    occurrence_id,
)
from project_kb.indexing.models import RepoState, ScanFacts, ScanPolicy
from project_kb.indexing.module_map import (
    MODULE_MAP_VERSION,
    ModuleStateInvariantCode,
    module_state_invariant_code,
)
from project_kb.indexing.policy import (
    hard_secret_reason,
    normalize_relative_path,
    path_key,
    policy_metadata,
)
from project_kb.snapshot.schema_v2 import (
    REQUIRED_INDEXES_V2,
    REQUIRED_TABLE_COLUMNS_V2,
    SCHEMA_SQL_V2,
)

LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
LEGACY_SCANNER_VERSION = "stage4-v0-3"
LEGACY_POLICY_VERSION = "stage4-v0-3"
LEGACY_EXPECTED_EXTRACTORS = {"python-ast": "1"}
SCANNER_VERSION = "stage6-v2"
PROOF_CONTRACT_VERSION = "3"
VERIFIER_VERSION = "3"
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

REQUIRED_TABLE_COLUMNS_V1 = REQUIRED_TABLE_COLUMNS
REQUIRED_INDEXES_V1 = REQUIRED_INDEXES


def write_snapshot(
    path: Path,
    *,
    snapshot_id: str,
    run_id: str,
    project_id: str,
    repo_root_norm: str,
    repository_identity_hash: str,
    repository_binding_generation: str,
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
    file_id_map = {
        item.file_id: _require_identity(item.file_occurrence_id, "file") for item in facts.files
    }
    symbol_id_map = {
        item.symbol_id: _require_identity(item.occurrence_id, "symbol") for item in facts.symbols
    }
    verification_scope = {
        "included": ["safe_text_content", "candidate_population", "bounded_metadata"],
        "excluded": ["hard_secret_content", "ignored_content", "pruned_content"],
        "hard_secret_content_hashed": False,
    }
    write_start = time.perf_counter_ns()
    try:
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(SCHEMA_SQL_V2)
            conn.execute(
                """INSERT INTO snapshot_meta (
                       snapshot_id, project_id, run_id, schema_version, scanner_version,
                       policy_version, policy_json, extractor_versions_json, created_at,
                       repo_root_norm, repository_identity_hash,
                       repository_binding_generation, git_head, git_branch,
                       git_status_fingerprint, repo_state_before_json, repo_state_after_json,
                       build_status, logical_fingerprint, observation_fingerprint,
                       proof_contract_version, verifier_version, module_map_version,
                       occurrence_contract_version, verification_scope_json
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    repository_identity_hash,
                    repository_binding_generation,
                    before.head,
                    before.branch,
                    before.status_fingerprint,
                    json.dumps(before.to_dict(), sort_keys=True),
                    json.dumps(after.to_dict(), sort_keys=True),
                    "BUILDING",
                    logical_fingerprint,
                    observation_fingerprint,
                    PROOF_CONTRACT_VERSION,
                    VERIFIER_VERSION,
                    facts.module_map_version or MODULE_MAP_VERSION,
                    OCCURRENCE_CONTRACT_VERSION,
                    json.dumps(verification_scope, sort_keys=True),
                ),
            )
            conn.executemany(
                """INSERT INTO files (
                       file_id, legacy_file_id, snapshot_id, relative_path, path_key,
                       git_population, file_kind, language, extension, size_bytes, mtime_ns,
                       line_count, encoding, content_hash, analysis_level,
                       classification_reason, parse_status, source_root_id, source_root_path,
                       source_root_origin, module_name, module_resolution_status,
                       module_candidates_json, is_importable
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )""",
                [
                    (
                        file_id_map[item.file_id],
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
                        item.source_root_id,
                        item.source_root_path,
                        item.source_root_origin,
                        item.module_name,
                        item.module_resolution_status or "NOT_IMPORTABLE",
                        json.dumps(item.module_candidates),
                        int(item.is_importable),
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
                """INSERT INTO symbols (
                       symbol_id, legacy_symbol_id, snapshot_id, file_id, qualified_name,
                       canonical_qualified_name, logical_key, module_name, short_name,
                       symbol_kind, binding_role, start_line, end_line, start_column,
                       end_column, parent_symbol_id, signature_text, occurrence_ordinal,
                       occurrence_contract_version, extractor_name, extractor_version
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   )""",
                [
                    (
                        symbol_id_map[item.symbol_id],
                        item.symbol_id,
                        snapshot_id,
                        file_id_map[item.file_id],
                        item.qualified_name,
                        item.canonical_qualified_name,
                        item.logical_key,
                        item.module_name,
                        item.short_name,
                        item.symbol_kind,
                        item.binding_role,
                        item.start_line,
                        item.end_line,
                        item.start_column,
                        item.end_column,
                        symbol_id_map.get(item.parent_symbol_id),
                        item.signature_text,
                        item.occurrence_ordinal,
                        item.occurrence_contract_version or OCCURRENCE_CONTRACT_VERSION,
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                    )
                    for item in facts.symbols
                ],
            )
            conn.executemany(
                """INSERT INTO imports (
                       import_id, legacy_import_id, snapshot_id, file_id, import_kind,
                       module_text, imported_name, alias, relative_level, start_line, end_line,
                       resolution_status, resolved_file_id, normalized_module_name,
                       v2_resolution_status, v2_resolved_file_id, extractor_name,
                       extractor_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        stable_id(snapshot_id, "import", item.import_id),
                        item.import_id,
                        snapshot_id,
                        file_id_map[item.file_id],
                        item.import_kind,
                        item.module_text,
                        item.imported_name,
                        item.alias,
                        item.relative_level,
                        item.start_line,
                        item.end_line,
                        item.resolution_status,
                        file_id_map.get(item.resolved_file_id),
                        item.normalized_module_name,
                        item.v2_resolution_status or "UNRESOLVED",
                        item.v2_resolved_file_occurrence_id,
                        EXTRACTOR_NAME,
                        EXTRACTOR_VERSION,
                    )
                    for item in facts.imports
                ],
            )
            conn.executemany(
                """INSERT INTO relations (
                       relation_id, legacy_relation_id, snapshot_id, relation_kind,
                       source_file_id, source_symbol_id, target_file_id, target_symbol_id,
                       target_text, start_line, end_line, start_column, end_column,
                       resolution_status, evidence_kind, extractor_name, extractor_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        stable_id(snapshot_id, "relation", item.relation_id),
                        item.relation_id,
                        snapshot_id,
                        item.relation_kind,
                        file_id_map.get(item.source_file_id),
                        symbol_id_map.get(item.source_symbol_id),
                        file_id_map.get(item.target_file_id),
                        symbol_id_map.get(item.target_symbol_id),
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
                        file_id_map[item.file_id],
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
            conn.executemany(
                """INSERT INTO proof_manifest (
                       proof_id, snapshot_id, file_id, relative_path, path_key,
                       git_population, proof_class, evidence_kind, stat_signature_json,
                       content_hash, verification_scope, exclusion_reason
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _proof_rows(
                    snapshot_id=snapshot_id,
                    facts=facts,
                    file_id_map=file_id_map,
                ),
            )
            database_write_ms = (time.perf_counter_ns() - write_start) // 1_000_000
            conn.execute(
                """INSERT INTO index_runs (
                       run_id, project_id, started_at, finished_at, status, failure_code,
                       candidate_count, candidate_bytes, metadata_only_count,
                       pruned_root_count, text_file_count, parsed_file_count,
                       parse_failure_count, symbol_count, import_count, relation_count,
                       enumeration_ms, classification_ms, read_hash_ms, parse_ms,
                       relations_ms, database_write_ms, total_ms, snapshot_size_bytes,
                       published_snapshot_id, warnings_json
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?
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


def _require_identity(value: str | None, kind: str) -> str:
    if value is None:
        raise IndexingError(
            "Semantic v2 snapshot is missing a snapshot-bound occurrence identity.",
            details={"identity_kind": kind},
        )
    return value


def _proof_rows(
    *,
    snapshot_id: str,
    facts: ScanFacts,
    file_id_map: dict[str, str],
) -> list[tuple[Any, ...]]:
    files_by_key = {item.path_key: item for item in facts.files}
    pruned_by_key = {path_key(item.relative_path): item for item in facts.pruned_roots}
    rows: list[tuple[Any, ...]] = []
    for key, evidence in sorted(facts.evidence.items()):
        file = files_by_key.get(key)
        pruned = pruned_by_key.get(key)
        if evidence.evidence_kind == "TEXT":
            proof_class = "CONTENT_HASH"
            scope = "STRUCTURAL_CONTENT"
            exclusion_reason = None
        elif evidence.evidence_kind == "HARD_SECRET":
            proof_class = "EXCLUDED_HARD_SECRET"
            scope = "EXCLUDED"
            exclusion_reason = file.classification_reason if file else "hard_secret"
        elif evidence.evidence_kind in {"PRUNED_ROOT", "REDIRECTED_PRUNED_ROOT"}:
            proof_class = "EXCLUDED_PRUNED"
            scope = "EXCLUDED"
            exclusion_reason = pruned.reason if pruned else "pruned_root"
        else:
            proof_class = "BOUNDED_METADATA"
            scope = "METADATA_ONLY"
            exclusion_reason = file.classification_reason if file else None
        rows.append(
            (
                stable_id(snapshot_id, "proof", key),
                snapshot_id,
                file_id_map.get(file.file_id) if file else None,
                evidence.relative_path,
                key,
                file.git_population if file else "POLICY_DISCOVERED",
                proof_class,
                evidence.evidence_kind,
                json.dumps(evidence.stat_signature)
                if evidence.stat_signature is not None
                else None,
                evidence.content_hash if proof_class == "CONTENT_HASH" else None,
                scope,
                exclusion_reason,
            )
        )
    return rows


def validate_snapshot(
    path: Path,
    *,
    project_id: str | None = None,
    expected_policy_version: str | None = None,
    expected_repo_root_norm: str | None = None,
    expected_repository_identity_hash: str | None = None,
    expected_repository_binding_generation: str | None = None,
    require_v2: bool = False,
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
            version = _identify_snapshot_version(conn)
            if version not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
                raise SnapshotQueryError(
                    "Snapshot version is not supported by this tool.",
                    code="SNAPSHOT_REBUILD_REQUIRED",
                    details={
                        "snapshot_classification": "incompatible_version",
                        "schema_version": version,
                        "compatibility": {
                            "readable": True,
                            "schema_compatible": False,
                            "scanner_compatible": False,
                            "policy_compatible": False,
                            "extractor_compatible": False,
                        },
                    },
                )
            required_columns = (
                REQUIRED_TABLE_COLUMNS_V1
                if version == LEGACY_SCHEMA_VERSION
                else REQUIRED_TABLE_COLUMNS_V2
            )
            required_indexes = (
                REQUIRED_INDEXES_V1 if version == LEGACY_SCHEMA_VERSION else REQUIRED_INDEXES_V2
            )
            _validate_schema_manifest(
                conn,
                required_table_columns=required_columns,
                required_indexes=required_indexes,
            )
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
            _validate_repository_binding(
                row,
                version=version,
                expected_repo_root_norm=expected_repo_root_norm,
                expected_repository_identity_hash=expected_repository_identity_hash,
                expected_repository_binding_generation=expected_repository_binding_generation,
            )
            compatibility = _compatibility(
                row,
                version=version,
                expected_policy_version=expected_policy_version or ScanPolicy().policy_version,
            )
            compatibility_checks = (
                "schema_compatible",
                "scanner_compatible",
                "policy_compatible",
                "extractor_compatible",
            )
            if version == SCHEMA_VERSION:
                compatibility_checks += (
                    "proof_compatible",
                    "verifier_compatible",
                    "module_map_compatible",
                    "occurrence_compatible",
                )
            if not all(compatibility[key] for key in compatibility_checks):
                raise SnapshotQueryError(
                    "Snapshot semantics are incompatible with the current scanner.",
                    code="SNAPSHOT_REBUILD_REQUIRED",
                    details={"compatibility": compatibility},
                )
            _validate_snapshot_invariants(conn, row)
            if version == SCHEMA_VERSION:
                _validate_v2_invariants(conn, row)
            elif require_v2:
                raise SnapshotQueryError(
                    "Legacy v1 snapshot requires a full reindex for this v2 operation.",
                    code="SNAPSHOT_REBUILD_REQUIRED",
                    details={
                        "snapshot_classification": "valid_v1",
                        "reason": "legacy_v1_reindex_required",
                        "full_reindex_required": True,
                    },
                )
            result = dict(row)
            result["extractor_versions"] = json.loads(row["extractor_versions_json"])
            result["compatibility"] = compatibility
            result["snapshot_classification"] = (
                "valid_v1" if version == LEGACY_SCHEMA_VERSION else "valid_v2"
            )
            return result
    except SnapshotQueryError:
        raise
    except (OSError, sqlite3.Error, KeyError) as exc:
        raise SnapshotQueryError(
            "Snapshot could not be read safely.",
            code="SNAPSHOT_STORAGE_ERROR",
            details={"error_type": type(exc).__name__},
        ) from exc


def _identify_snapshot_version(conn: sqlite3.Connection) -> int:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "snapshot_meta" not in tables:
        raise SnapshotQueryError(
            "Snapshot has no version-identification metadata.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(snapshot_meta)")}
    if "schema_version" not in columns:
        raise SnapshotQueryError(
            "Snapshot version-identification metadata is incomplete.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    rows = conn.execute("SELECT schema_version FROM snapshot_meta").fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], int) or isinstance(rows[0][0], bool):
        raise SnapshotQueryError(
            "Snapshot version-identification metadata is invalid.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    return rows[0][0]


def _validate_schema_manifest(
    conn: sqlite3.Connection,
    *,
    required_table_columns: dict[str, set[str]],
    required_indexes: set[str],
) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing_tables = set(required_table_columns) - tables
    missing_columns: dict[str, list[str]] = {}
    for table, required in required_table_columns.items():
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
    missing_indexes = sorted(required_indexes - indexes)
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
    version: int,
    expected_policy_version: str,
) -> dict[str, bool]:
    try:
        extractors = json.loads(row["extractor_versions_json"])
    except TypeError, json.JSONDecodeError:
        extractors = None
    expected_scanner = (
        LEGACY_SCANNER_VERSION if version == LEGACY_SCHEMA_VERSION else SCANNER_VERSION
    )
    expected_policy = (
        LEGACY_POLICY_VERSION if version == LEGACY_SCHEMA_VERSION else expected_policy_version
    )
    expected_extractors = (
        LEGACY_EXPECTED_EXTRACTORS if version == LEGACY_SCHEMA_VERSION else EXPECTED_EXTRACTORS
    )
    compatibility = {
        "readable": True,
        "schema_compatible": row["schema_version"] == version,
        "scanner_compatible": row["scanner_version"] == expected_scanner,
        "policy_compatible": row["policy_version"] == expected_policy,
        "extractor_compatible": extractors == expected_extractors,
    }
    if version == LEGACY_SCHEMA_VERSION:
        compatibility["current"] = False
    else:
        compatibility.update(
            {
                "proof_compatible": row["proof_contract_version"] == PROOF_CONTRACT_VERSION,
                "verifier_compatible": row["verifier_version"] == VERIFIER_VERSION,
                "module_map_compatible": row["module_map_version"] == MODULE_MAP_VERSION,
                "occurrence_compatible": row["occurrence_contract_version"]
                == OCCURRENCE_CONTRACT_VERSION,
            }
        )
    return compatibility


def _validate_repository_binding(
    row: sqlite3.Row,
    *,
    version: int,
    expected_repo_root_norm: str | None,
    expected_repository_identity_hash: str | None,
    expected_repository_binding_generation: str | None,
) -> None:
    mismatch: dict[str, str] = {}
    if expected_repo_root_norm is not None and row["repo_root_norm"] != expected_repo_root_norm:
        mismatch["repo_root_norm"] = "mismatch"
    if (
        version == SCHEMA_VERSION
        and expected_repository_identity_hash is not None
        and row["repository_identity_hash"] != expected_repository_identity_hash
    ):
        mismatch["repository_identity_hash"] = "mismatch"
    if (
        version == SCHEMA_VERSION
        and expected_repository_binding_generation is not None
        and row["repository_binding_generation"] != expected_repository_binding_generation
    ):
        mismatch["repository_binding_generation"] = "mismatch"
    if mismatch:
        raise SnapshotQueryError(
            "Snapshot is bound to a different active repository.",
            code="SNAPSHOT_REBUILD_REQUIRED",
            details={
                "snapshot_classification": "wrong_repository_binding",
                "binding": mismatch,
            },
        )


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


def _validate_v2_invariants(conn: sqlite3.Connection, meta: sqlite3.Row) -> None:
    snapshot_id = meta["snapshot_id"]
    if not _is_sha256_hex(meta["repository_identity_hash"]) or not _is_binding_generation(
        meta["repository_binding_generation"]
    ):
        raise SnapshotQueryError(
            "Snapshot repository binding is invalid.", code="SNAPSHOT_STORAGE_ERROR"
        )
    file_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()[0]
    files_with_proof = conn.execute(
        """SELECT COUNT(*) FROM files f
           WHERE f.snapshot_id = ? AND EXISTS (
               SELECT 1 FROM proof_manifest p
               WHERE p.snapshot_id = f.snapshot_id AND p.path_key = f.path_key
           )""",
        (snapshot_id,),
    ).fetchone()[0]
    if files_with_proof != file_count:
        raise SnapshotQueryError(
            "Snapshot proof manifest does not cover every persisted file occurrence.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    files_without_exact_proof = conn.execute(
        """SELECT COUNT(*) FROM files f
           WHERE f.snapshot_id = ? AND (
               SELECT COUNT(*) FROM proof_manifest p
               WHERE p.snapshot_id = f.snapshot_id AND p.path_key = f.path_key
           ) != 1""",
        (snapshot_id,),
    ).fetchone()[0]
    if files_without_exact_proof:
        raise SnapshotQueryError(
            "Snapshot proof manifest does not bind each file exactly once.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    invalid_proof = conn.execute(
        """SELECT COUNT(*) FROM proof_manifest
           WHERE snapshot_id != ?
              OR proof_class NOT IN (
                   'CONTENT_HASH', 'BOUNDED_METADATA',
                   'EXCLUDED_HARD_SECRET', 'EXCLUDED_PRUNED'
                 )
              OR (proof_class = 'CONTENT_HASH' AND content_hash IS NULL)
              OR (proof_class != 'CONTENT_HASH' AND content_hash IS NOT NULL)
              OR (proof_class = 'EXCLUDED_HARD_SECRET' AND content_hash IS NOT NULL)""",
        (snapshot_id,),
    ).fetchone()[0]
    if invalid_proof:
        raise SnapshotQueryError(
            "Snapshot proof-manifest hash invariants are invalid.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    proof_count, distinct_proof_paths = conn.execute(
        """SELECT COUNT(*), COUNT(DISTINCT path_key)
           FROM proof_manifest WHERE snapshot_id = ?""",
        (snapshot_id,),
    ).fetchone()
    if proof_count != distinct_proof_paths:
        raise SnapshotQueryError(
            "Snapshot proof-manifest population invariants are invalid.",
            code="SNAPSHOT_STORAGE_ERROR",
        )
    _validate_v2_json_contracts(conn, meta)
    _validate_v2_module_and_occurrence_contracts(conn, snapshot_id)


class _V2InvariantViolation(ValueError):
    def __init__(self, validation_area: str, invariant_code: str) -> None:
        super().__init__(invariant_code)
        self.validation_area = validation_area
        self.invariant_code = invariant_code


def _validate_v2_module_and_occurrence_contracts(
    conn: sqlite3.Connection,
    snapshot_id: str,
) -> None:
    try:
        file_rows = conn.execute("SELECT * FROM files ORDER BY path_key").fetchall()
        exact_modules: set[str] = set()
        files_by_id: dict[str, sqlite3.Row] = {}
        for row in file_rows:
            try:
                relative_path = normalize_relative_path(row["relative_path"])
            except (TypeError, ValueError) as exc:
                raise _V2InvariantViolation(
                    "FILE_OCCURRENCE",
                    "FILE_PATH_IDENTITY_INVALID",
                ) from exc
            canonical_path_key = path_key(relative_path)
            if relative_path != row["relative_path"] or canonical_path_key != row["path_key"]:
                raise _V2InvariantViolation(
                    "FILE_OCCURRENCE",
                    "FILE_PATH_IDENTITY_INVALID",
                )
            if row["legacy_file_id"] != stable_id(canonical_path_key):
                raise _V2InvariantViolation(
                    "FILE_OCCURRENCE",
                    "FILE_LEGACY_ID_INVALID",
                )
            expected_file_id = file_occurrence_id(snapshot_id, relative_path)
            if row["file_id"] != expected_file_id or row["file_id"] == row["legacy_file_id"]:
                raise _V2InvariantViolation(
                    "FILE_OCCURRENCE",
                    "FILE_OCCURRENCE_ID_INVALID",
                )
            files_by_id[row["file_id"]] = row

            try:
                candidates = json.loads(row["module_candidates_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise _V2InvariantViolation(
                    "MODULE_MAP",
                    ModuleStateInvariantCode.CANDIDATES_INVALID,
                ) from exc
            status = row["module_resolution_status"]
            invariant_code = module_state_invariant_code(
                source_root_id_value=row["source_root_id"],
                source_root_path=row["source_root_path"],
                source_root_origin=row["source_root_origin"],
                module_name=row["module_name"],
                resolution_status=status,
                module_candidates=candidates,
                is_importable=row["is_importable"],
            )
            if invariant_code is not None:
                raise _V2InvariantViolation("MODULE_MAP", invariant_code)
            if status == "EXACT":
                module_name = row["module_name"]
                if module_name in exact_modules:
                    raise _V2InvariantViolation(
                        "MODULE_MAP",
                        ModuleStateInvariantCode.EXACT_DUPLICATE_INVALID,
                    )
                exact_modules.add(module_name)

        symbol_rows = conn.execute(
            """SELECT s.*, f.relative_path AS file_relative_path,
                      f.legacy_file_id AS file_legacy_id,
                      f.module_name AS file_module_name,
                      f.module_resolution_status AS file_module_status
               FROM symbols s JOIN files f ON f.file_id = s.file_id
               ORDER BY s.symbol_id"""
        ).fetchall()
        for row in symbol_rows:
            ordinal = row["occurrence_ordinal"]
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
                raise _V2InvariantViolation(
                    "SYMBOL_OCCURRENCE",
                    "SYMBOL_OCCURRENCE_ID_INVALID",
                )
            expected_symbol_id = occurrence_id(
                snapshot_id=snapshot_id,
                relative_path=row["file_relative_path"],
                entity_kind=row["symbol_kind"],
                binding_role=row["binding_role"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                start_column=row["start_column"],
                end_column=row["end_column"],
                ordinal=ordinal,
            )
            expected_legacy_id = (
                stable_id(row["file_legacy_id"], "module")
                if row["symbol_kind"] == "MODULE"
                else stable_id(
                    row["file_legacy_id"],
                    row["qualified_name"],
                    row["symbol_kind"],
                    row["start_line"],
                    row["start_column"],
                )
            )
            if (
                row["symbol_id"] != expected_symbol_id
                or row["legacy_symbol_id"] != expected_legacy_id
                or row["symbol_id"] == row["legacy_symbol_id"]
                or row["occurrence_contract_version"] != OCCURRENCE_CONTRACT_VERSION
            ):
                raise _V2InvariantViolation(
                    "SYMBOL_OCCURRENCE",
                    "SYMBOL_OCCURRENCE_ID_INVALID",
                )
            is_module = row["symbol_kind"] == "MODULE"
            if (
                is_module
                and (row["binding_role"] != "MODULE" or row["parent_symbol_id"] is not None)
            ) or (
                not is_module
                and (row["binding_role"] != "DECLARATION" or row["parent_symbol_id"] is None)
            ):
                raise _V2InvariantViolation(
                    "SYMBOL_OCCURRENCE",
                    "SYMBOL_BINDING_CONTRACT_INVALID",
                )
            if row["file_module_status"] == "EXACT":
                module_name = row["file_module_name"]
                canonical_name = row["canonical_qualified_name"]
                if (
                    row["module_name"] != module_name
                    or not isinstance(canonical_name, str)
                    or (
                        canonical_name != module_name
                        if is_module
                        else not canonical_name.startswith(f"{module_name}.")
                    )
                    or row["logical_key"]
                    != logical_symbol_key(
                        module_name=module_name,
                        lexical_name=canonical_name,
                        entity_kind=row["symbol_kind"],
                        binding_role=row["binding_role"],
                    )
                ):
                    raise _V2InvariantViolation(
                        "SYMBOL_OCCURRENCE",
                        "SYMBOL_CANONICAL_IDENTITY_INVALID",
                    )
            elif any(
                row[field] is not None
                for field in ("module_name", "canonical_qualified_name", "logical_key")
            ):
                raise _V2InvariantViolation(
                    "SYMBOL_OCCURRENCE",
                    "SYMBOL_CANONICAL_IDENTITY_INVALID",
                )

        for row in conn.execute(
            """SELECT i.*, target.module_name AS target_module_name
               FROM imports i
               LEFT JOIN files target ON target.file_id = i.v2_resolved_file_id"""
        ):
            if row["import_id"] != stable_id(snapshot_id, "import", row["legacy_import_id"]):
                raise _V2InvariantViolation(
                    "IMPORT_TARGET",
                    "IMPORT_TARGET_CONTRACT_INVALID",
                )
            if row["file_id"] not in files_by_id:
                raise _V2InvariantViolation(
                    "IMPORT_TARGET",
                    "IMPORT_TARGET_CONTRACT_INVALID",
                )
            status = row["v2_resolution_status"]
            if status not in {"EXACT", "AMBIGUOUS", "UNRESOLVED", "EXTERNAL"}:
                raise _V2InvariantViolation(
                    "IMPORT_TARGET",
                    "IMPORT_TARGET_CONTRACT_INVALID",
                )
            if status == "EXACT":
                if (
                    row["v2_resolved_file_id"] is None
                    or not row["normalized_module_name"]
                    or row["target_module_name"] != row["normalized_module_name"]
                ):
                    raise _V2InvariantViolation(
                        "IMPORT_TARGET",
                        "IMPORT_TARGET_CONTRACT_INVALID",
                    )
            elif row["v2_resolved_file_id"] is not None:
                raise _V2InvariantViolation(
                    "IMPORT_TARGET",
                    "IMPORT_TARGET_CONTRACT_INVALID",
                )

        for row in conn.execute("SELECT relation_id, legacy_relation_id FROM relations"):
            if row["relation_id"] != stable_id(
                snapshot_id,
                "relation",
                row["legacy_relation_id"],
            ):
                raise _V2InvariantViolation(
                    "RELATION_OCCURRENCE",
                    "RELATION_OCCURRENCE_ID_INVALID",
                )
        for row in conn.execute("SELECT proof_id, path_key FROM proof_manifest"):
            if row["proof_id"] != stable_id(snapshot_id, "proof", row["path_key"]):
                raise _V2InvariantViolation(
                    "PROOF_OCCURRENCE",
                    "PROOF_OCCURRENCE_ID_INVALID",
                )
        pruned_paths = {
            path_key(normalize_relative_path(row["relative_path"]))
            for row in conn.execute("SELECT relative_path FROM pruned_roots")
        }
        proof_pruned_paths = {
            row["path_key"]
            for row in conn.execute(
                "SELECT path_key FROM proof_manifest WHERE proof_class = 'EXCLUDED_PRUNED'"
            )
        }
        if pruned_paths != proof_pruned_paths:
            raise _V2InvariantViolation(
                "PROOF_OCCURRENCE",
                "PROOF_PRUNED_COVERAGE_INVALID",
            )
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        validation_area = "MODULE_OCCURRENCE"
        invariant_code = "MODULE_OCCURRENCE_CONTRACT_INVALID"
        if isinstance(exc, _V2InvariantViolation):
            validation_area = exc.validation_area
            invariant_code = exc.invariant_code
        raise SnapshotQueryError(
            "Snapshot v2 module or occurrence contract is invalid.",
            code="SNAPSHOT_STORAGE_ERROR",
            details={
                "validation_area": validation_area,
                "invariant_code": invariant_code,
            },
        ) from exc


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_binding_generation(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_v2_json_contracts(conn: sqlite3.Connection, meta: sqlite3.Row) -> None:
    try:
        policy = json.loads(meta["policy_json"])
        scope = json.loads(meta["verification_scope_json"])
        before = json.loads(meta["repo_state_before_json"])
        after = json.loads(meta["repo_state_after_json"])
        if (
            not isinstance(policy, dict)
            or policy.get("policy_version") != meta["policy_version"]
            or any(
                not isinstance(policy.get(field), int)
                or isinstance(policy.get(field), bool)
                or policy[field] <= 0
                for field in ("max_text_bytes", "binary_probe_bytes")
            )
            or any(
                not isinstance(policy.get(field), list)
                or any(not isinstance(item, str) for item in policy[field])
                for field in ("discovered_metadata_paths", "discovered_pruned_roots")
            )
        ):
            raise ValueError("invalid policy metadata")
        if (
            not isinstance(scope, dict)
            or scope.get("hard_secret_content_hashed") is not False
            or any(
                not isinstance(scope.get(field), list)
                or any(not isinstance(item, str) for item in scope[field])
                for field in ("included", "excluded")
            )
            or "hard_secret_content" not in scope["excluded"]
        ):
            raise ValueError("invalid verification scope")
        required_state = {
            "head",
            "branch",
            "status_fingerprint",
            "candidate_fingerprint",
            "visibility_flags_before",
            "index_visibility_paths",
        }
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("invalid repository state")
        if not required_state.issubset(before) or not required_state.issubset(after):
            raise ValueError("incomplete repository state")
        for state in (before, after):
            if (
                (state["head"] is not None and not isinstance(state["head"], str))
                or (state["branch"] is not None and not isinstance(state["branch"], str))
                or not _is_sha256_hex(state["status_fingerprint"])
                or not _is_sha256_hex(state["candidate_fingerprint"])
                or not isinstance(state["visibility_flags_before"], list)
                or not isinstance(state["index_visibility_paths"], list)
                or any(
                    not isinstance(path, str)
                    for path in (
                        *state["visibility_flags_before"],
                        *state["index_visibility_paths"],
                    )
                )
                or state["visibility_flags_before"] != state["index_visibility_paths"]
            ):
                raise ValueError("invalid repository state values")
        if (
            before != after
            or meta["git_head"] != after["head"]
            or meta["git_branch"] != after["branch"]
            or meta["git_status_fingerprint"] != after["status_fingerprint"]
            or not _is_sha256_hex(meta["logical_fingerprint"])
            or not _is_sha256_hex(meta["observation_fingerprint"])
        ):
            raise ValueError("repository state is not bound to snapshot metadata")
        evidence_payload: list[dict[str, Any]] = []
        for row in conn.execute(
            """SELECT p.relative_path, p.path_key, p.file_id, p.proof_class,
                      p.evidence_kind, p.stat_signature_json, p.git_population,
                      p.content_hash AS proof_content_hash,
                      p.verification_scope, p.exclusion_reason,
                      f.relative_path AS file_relative_path, f.path_key AS file_path_key,
                      f.git_population AS file_git_population, f.file_kind,
                      f.analysis_level, f.content_hash AS file_content_hash
               FROM proof_manifest p
               LEFT JOIN files f ON f.file_id = p.file_id"""
        ):
            signature = (
                json.loads(row["stat_signature_json"])
                if row["stat_signature_json"] is not None
                else None
            )
            if signature is not None and (
                not isinstance(signature, list)
                or len(signature) != 5
                or any(not isinstance(value, int) or isinstance(value, bool) for value in signature)
            ):
                raise ValueError("invalid proof stat signature")
            proof_class = row["proof_class"]
            evidence_payload.append(
                {
                    "relative_path": row["relative_path"],
                    "evidence_kind": row["evidence_kind"],
                    "stat_signature": signature,
                    "content_hash": row["proof_content_hash"],
                }
            )
            if (
                normalize_relative_path(row["relative_path"]) != row["relative_path"]
                or path_key(row["relative_path"]) != row["path_key"]
            ):
                raise ValueError("invalid proof path identity")
            if row["file_id"] is not None and (
                row["relative_path"] != row["file_relative_path"]
                or row["path_key"] != row["file_path_key"]
                or row["git_population"] != row["file_git_population"]
            ):
                raise ValueError("proof/file identity mismatch")
            if proof_class == "CONTENT_HASH":
                content_hash = row["proof_content_hash"]
                if (
                    row["file_id"] is None
                    or row["evidence_kind"] != "TEXT"
                    or row["file_kind"] != "TEXT"
                    or row["analysis_level"] != "TEXT_STRUCTURAL"
                    or signature is None
                    or not _is_sha256_hex(content_hash)
                    or content_hash != row["file_content_hash"]
                    or row["verification_scope"] != "STRUCTURAL_CONTENT"
                    or row["exclusion_reason"] is not None
                ):
                    raise ValueError("invalid content proof")
            elif proof_class == "EXCLUDED_HARD_SECRET":
                if (
                    row["file_id"] is None
                    or row["evidence_kind"] != "HARD_SECRET"
                    or row["file_kind"] != "HARD_SECRET"
                    or row["analysis_level"] != "METADATA_ONLY"
                    or hard_secret_reason(row["relative_path"]) is None
                    or row["verification_scope"] != "EXCLUDED"
                    or not row["exclusion_reason"]
                ):
                    raise ValueError("invalid hard-secret exclusion")
            elif proof_class == "EXCLUDED_PRUNED":
                if (
                    row["file_id"] is not None
                    or row["evidence_kind"] not in {"PRUNED_ROOT", "REDIRECTED_PRUNED_ROOT"}
                    or row["verification_scope"] != "EXCLUDED"
                    or not row["exclusion_reason"]
                ):
                    raise ValueError("invalid pruned exclusion")
            elif proof_class == "BOUNDED_METADATA" and (
                row["file_id"] is None
                or row["evidence_kind"] != row["file_kind"]
                or row["analysis_level"] != "METADATA_ONLY"
                or row["verification_scope"] != "METADATA_ONLY"
            ):
                raise ValueError("invalid bounded metadata proof")
        observation_payload = {
            "repo_state": after,
            "evidence": sorted(
                evidence_payload,
                key=lambda item: item["relative_path"],
            ),
        }
        encoded_observation = json.dumps(
            observation_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(encoded_observation).hexdigest() != meta["observation_fingerprint"]:
            raise ValueError("snapshot observation fingerprint mismatch")
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        raise SnapshotQueryError(
            "Snapshot v2 proof metadata is invalid.",
            code="SNAPSHOT_STORAGE_ERROR",
        ) from exc


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
        value.pop("file_occurrence_id", None)
        files.append(value)
    payload = {
        "files": sorted(files, key=lambda item: item["path_key"]),
        "pruned_roots": sorted(
            (asdict(item) for item in facts.pruned_roots), key=lambda item: item["relative_path"]
        ),
        "symbols": sorted(
            (_logical_symbol_dict(item) for item in facts.symbols),
            key=lambda item: item["symbol_id"],
        ),
        "imports": sorted(
            (_logical_import_dict(item) for item in facts.imports),
            key=lambda item: item["import_id"],
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


def _logical_symbol_dict(item: Any) -> dict[str, Any]:
    value = asdict(item)
    value.pop("occurrence_id", None)
    value.pop("parent_occurrence_id", None)
    return value


def _logical_import_dict(item: Any) -> dict[str, Any]:
    value = asdict(item)
    value.pop("v2_resolved_file_occurrence_id", None)
    return value


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
    def __init__(
        self,
        path: Path,
        *,
        project_id: str,
        expected_repo_root_norm: str | None = None,
        expected_repository_identity_hash: str | None = None,
        expected_repository_binding_generation: str | None = None,
        active_binding: Callable[[], tuple[str, str] | tuple[str, str, str]] | None = None,
    ) -> None:
        self.path = path
        self.expected_repo_root_norm = expected_repo_root_norm
        self.expected_repository_identity_hash = expected_repository_identity_hash
        self.expected_repository_binding_generation = expected_repository_binding_generation
        self.active_binding = active_binding
        self.meta = validate_snapshot(
            path,
            project_id=project_id,
            expected_repo_root_norm=expected_repo_root_norm,
            expected_repository_identity_hash=expected_repository_identity_hash,
            expected_repository_binding_generation=expected_repository_binding_generation,
        )
        self._validate_active_binding()

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
            self._validate_active_binding()
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
                rows = [dict(row) for row in conn.execute(sql, values).fetchall()]
            self._validate_active_binding()
            return rows
        except SnapshotQueryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise SnapshotQueryError(
                "Snapshot query failed.",
                code="SNAPSHOT_STORAGE_ERROR",
                details={"error_type": type(exc).__name__},
            ) from exc

    def _validate_active_binding(self) -> None:
        if self.active_binding is None:
            return
        binding = self.active_binding()
        repo_root_norm, identity_hash = binding[:2]
        binding_generation = binding[2] if len(binding) == 3 else None
        root_mismatch = (
            self.expected_repo_root_norm is not None
            and repo_root_norm != self.expected_repo_root_norm
        )
        identity_mismatch = (
            self.meta["schema_version"] == SCHEMA_VERSION
            and self.expected_repository_identity_hash is not None
            and identity_hash != self.expected_repository_identity_hash
        )
        generation_mismatch = (
            self.meta["schema_version"] == SCHEMA_VERSION
            and self.expected_repository_binding_generation is not None
            and binding_generation != self.expected_repository_binding_generation
        )
        if root_mismatch or identity_mismatch or generation_mismatch:
            raise SnapshotQueryError(
                "Active repository binding changed during snapshot access.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={
                    "snapshot_classification": "wrong_repository_binding",
                    "reason": "active_binding_changed_during_query",
                },
            )


def _query_path_key(path: str) -> str:
    try:
        return path_key(normalize_relative_path(path))
    except ValueError as exc:
        raise SnapshotQueryError(
            "Snapshot query requires a safe repository-relative path.",
            code="INVALID_RELATIVE_PATH",
            details={"file": path},
        ) from exc
