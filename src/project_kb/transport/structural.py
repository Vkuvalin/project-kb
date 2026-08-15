"""Persistent, bounded structural reads over one pinned Project KB snapshot."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from project_kb.errors import SnapshotQueryError
from project_kb.indexing.policy import normalize_relative_path, path_key
from project_kb.snapshot import SnapshotReader

DatasetName = Literal["files", "symbols", "imports", "relations", "parse_diagnostics"]
OperationName = Literal["rows", "count", "distinct", "group_count", "batch_lookup"]
FilterOperation = Literal["eq", "prefix", "in", "is_null"]

DEFAULT_RESPONSE_BYTE_LIMIT = 65_536
MIN_RESPONSE_BYTE_LIMIT = 1_024
MAX_RESPONSE_BYTE_LIMIT = 262_144
MAX_RECORD_LIMIT = 500
MAX_FILTERS = 8
MAX_FILTER_VALUES = 100
MAX_BATCH_SYMBOLS = 50
MAX_PATHS = 20
MAX_PATH_LENGTH = 512
MAX_GROUP_FIELDS = 3
CURSOR_VERSION = 1


class QueryBoundaryError(RuntimeError):
    """Stable failure raised by the bounded structural transport boundary."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FrozenSnapshotConfig:
    """Operator-provided identity for one immutable structural snapshot."""

    path: Path
    project_id: str
    snapshot_id: str
    artifact_sha256: str
    source_commit: str

    @classmethod
    def from_environment(cls) -> FrozenSnapshotConfig:
        required = {
            "path": "PKB_MCP_SNAPSHOT_PATH",
            "project_id": "PKB_MCP_PROJECT_ID",
            "snapshot_id": "PKB_MCP_SNAPSHOT_ID",
            "artifact_sha256": "PKB_MCP_SNAPSHOT_SHA256",
            "source_commit": "PKB_MCP_SOURCE_COMMIT",
        }
        values: dict[str, str] = {}
        missing: list[str] = []
        for field, variable in required.items():
            value = os.environ.get(variable)
            if value:
                values[field] = value
            else:
                missing.append(variable)
        if missing:
            raise QueryBoundaryError(
                f"Missing required MCP snapshot settings: {', '.join(sorted(missing))}.",
                code="MCP_SNAPSHOT_CONFIG_MISSING",
            )
        return cls(
            path=Path(values["path"]),
            project_id=values["project_id"],
            snapshot_id=values["snapshot_id"],
            artifact_sha256=values["artifact_sha256"],
            source_commit=values["source_commit"],
        )


@dataclass(frozen=True)
class _DatasetSpec:
    sql: str
    columns: tuple[str, ...]
    order_by: tuple[str, ...]


@dataclass(frozen=True)
class _PageCandidate:
    item_key: str | None
    items: list[dict[str, Any]]
    total: int
    offset: int
    cursor_identity: str | None
    extra: dict[str, Any]
    paginate: bool = True
    records_counter: Callable[[Sequence[dict[str, Any]]], int] = len


FILE_COLUMNS = (
    "file_id",
    "legacy_file_id",
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
    "source_root_id",
    "source_root_path",
    "source_root_origin",
    "module_name",
    "module_resolution_status",
    "module_candidates_json",
    "is_importable",
)
SYMBOL_COLUMNS = (
    "relative_path",
    "path_key",
    "content_hash",
    "symbol_id",
    "legacy_symbol_id",
    "qualified_name",
    "canonical_qualified_name",
    "logical_key",
    "module_name",
    "short_name",
    "symbol_kind",
    "binding_role",
    "start_line",
    "end_line",
    "start_column",
    "end_column",
    "parent_symbol_id",
    "signature_text",
    "occurrence_ordinal",
    "occurrence_contract_version",
    "extractor_name",
    "extractor_version",
)
IMPORT_COLUMNS = (
    "relative_path",
    "path_key",
    "content_hash",
    "import_id",
    "legacy_import_id",
    "import_kind",
    "module_text",
    "imported_name",
    "alias",
    "relative_level",
    "start_line",
    "end_line",
    "resolution_status",
    "resolved_relative_path",
    "normalized_module_name",
    "v2_resolution_status",
    "v2_resolved_relative_path",
    "extractor_name",
    "extractor_version",
)
RELATION_COLUMNS = (
    "relation_id",
    "legacy_relation_id",
    "relation_kind",
    "source_relative_path",
    "source_symbol_id",
    "target_relative_path",
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
)
DIAGNOSTIC_COLUMNS = (
    "diagnostic_id",
    "relative_path",
    "path_key",
    "diagnostic_kind",
    "message",
    "line",
    "column",
    "extractor_name",
    "extractor_version",
)


def _select(alias: str, columns: Sequence[str]) -> str:
    return ", ".join(f'{alias}."{column}" AS "{column}"' for column in columns)


DATASETS: Mapping[str, _DatasetSpec] = {
    "files": _DatasetSpec(
        sql=f"SELECT {_select('f', FILE_COLUMNS)} FROM files f",
        columns=FILE_COLUMNS,
        order_by=("path_key", "file_id"),
    ),
    "symbols": _DatasetSpec(
        sql=f"""SELECT
            f.relative_path AS relative_path,
            f.path_key AS path_key,
            f.content_hash AS content_hash,
            {_select("s", SYMBOL_COLUMNS[3:])}
            FROM symbols s JOIN files f ON f.file_id = s.file_id""",
        columns=SYMBOL_COLUMNS,
        order_by=("path_key", "start_line", "start_column", "symbol_id"),
    ),
    "imports": _DatasetSpec(
        sql="""SELECT
            f.relative_path AS relative_path,
            f.path_key AS path_key,
            f.content_hash AS content_hash,
            i.import_id AS import_id,
            i.legacy_import_id AS legacy_import_id,
            i.import_kind AS import_kind,
            i.module_text AS module_text,
            i.imported_name AS imported_name,
            i.alias AS alias,
            i.relative_level AS relative_level,
            i.start_line AS start_line,
            i.end_line AS end_line,
            i.resolution_status AS resolution_status,
            legacy_target.relative_path AS resolved_relative_path,
            i.normalized_module_name AS normalized_module_name,
            i.v2_resolution_status AS v2_resolution_status,
            v2_target.relative_path AS v2_resolved_relative_path,
            i.extractor_name AS extractor_name,
            i.extractor_version AS extractor_version
            FROM imports i JOIN files f ON f.file_id = i.file_id
            LEFT JOIN files legacy_target ON legacy_target.file_id = i.resolved_file_id
            LEFT JOIN files v2_target ON v2_target.file_id = i.v2_resolved_file_id""",
        columns=IMPORT_COLUMNS,
        order_by=("path_key", "start_line", "import_id"),
    ),
    "relations": _DatasetSpec(
        sql="""SELECT
            r.relation_id AS relation_id,
            r.legacy_relation_id AS legacy_relation_id,
            r.relation_kind AS relation_kind,
            source_file.relative_path AS source_relative_path,
            r.source_symbol_id AS source_symbol_id,
            target_file.relative_path AS target_relative_path,
            r.target_symbol_id AS target_symbol_id,
            r.target_text AS target_text,
            r.start_line AS start_line,
            r.end_line AS end_line,
            r.start_column AS start_column,
            r.end_column AS end_column,
            r.resolution_status AS resolution_status,
            r.evidence_kind AS evidence_kind,
            r.extractor_name AS extractor_name,
            r.extractor_version AS extractor_version
            FROM relations r
            LEFT JOIN files source_file ON source_file.file_id = r.source_file_id
            LEFT JOIN files target_file ON target_file.file_id = r.target_file_id""",
        columns=RELATION_COLUMNS,
        order_by=("relation_kind", "source_relative_path", "start_line", "relation_id"),
    ),
    "parse_diagnostics": _DatasetSpec(
        sql="""SELECT
            d.diagnostic_id AS diagnostic_id,
            f.relative_path AS relative_path,
            f.path_key AS path_key,
            d.diagnostic_kind AS diagnostic_kind,
            d.message AS message,
            d.line AS line,
            d.column AS column,
            d.extractor_name AS extractor_name,
            d.extractor_version AS extractor_version
            FROM parse_diagnostics d JOIN files f ON f.file_id = d.file_id""",
        columns=DIAGNOSTIC_COLUMNS,
        order_by=("path_key", "line", "column", "diagnostic_id"),
    ),
}


class StructuralTransportService:
    """One-process façade over one validated, source-pinned structural snapshot."""

    def __init__(
        self,
        config: FrozenSnapshotConfig,
        *,
        max_scan_records: int = 100_000,
        max_vm_steps: int = 5_000_000,
    ) -> None:
        self.config = config
        self.max_scan_records = max_scan_records
        self.max_vm_steps = max_vm_steps
        self.process_id = os.getpid()
        self.server_started_monotonic_ns = time.perf_counter_ns()
        self.process_start_count = 1
        self.bootstrap_count = 1
        self.snapshot_validation_count = 1
        self._call_count = 0
        self._artifact_sha256 = _sha256_file(config.path)
        if self._artifact_sha256 != config.artifact_sha256:
            raise QueryBoundaryError(
                "Frozen snapshot SHA-256 mismatch.",
                code="FROZEN_SNAPSHOT_HASH_MISMATCH",
            )
        self.reader = SnapshotReader(config.path, project_id=config.project_id, active_binding=None)
        if self.reader.meta.get("snapshot_id") != config.snapshot_id:
            raise QueryBoundaryError(
                "Frozen snapshot identity mismatch.",
                code="FROZEN_SNAPSHOT_ID_MISMATCH",
            )
        if self.reader.meta.get("git_head") != config.source_commit:
            raise QueryBoundaryError(
                "Frozen snapshot source commit mismatch.",
                code="FROZEN_SOURCE_COMMIT_MISMATCH",
            )
        if self.reader.meta.get("schema_version") != 2:
            raise QueryBoundaryError(
                "The structural MCP adapter requires a schema-v2 snapshot.",
                code="FROZEN_SNAPSHOT_SCHEMA_UNSUPPORTED",
            )
        self.source_ref = {
            "project_id": config.project_id,
            "snapshot_id": config.snapshot_id,
            "source_commit": config.source_commit,
            "schema_version": self.reader.meta["schema_version"],
            "artifact_sha256": config.artifact_sha256,
        }
        self.server_id = f"{config.snapshot_id}:{self.process_id}"

    def get_snapshot_metadata(
        self,
        *,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> str:
        arguments = {"response_byte_limit": response_byte_limit}

        def produce() -> _PageCandidate:
            meta = self.reader.meta
            metadata = {
                "project_id": self.config.project_id,
                "snapshot_id": self.config.snapshot_id,
                "source_commit": self.config.source_commit,
                "artifact_sha256": self.config.artifact_sha256,
                "artifact_size_bytes": self.config.path.stat().st_size,
                "created_at": meta["created_at"],
                "schema_version": meta["schema_version"],
                "scanner_version": meta["scanner_version"],
                "policy_version": meta["policy_version"],
                "build_status": meta["build_status"],
                "logical_fingerprint": meta["logical_fingerprint"],
                "observation_fingerprint": meta["observation_fingerprint"],
            }
            return _PageCandidate(
                item_key=None,
                items=[],
                total=1,
                offset=0,
                cursor_identity=None,
                extra={"metadata": metadata},
                paginate=False,
            )

        return self._run_call(
            "get_snapshot_metadata",
            arguments,
            response_byte_limit=response_byte_limit,
            producer=produce,
            fixed_returned=1,
        )

    def find_symbols(
        self,
        *,
        names: Sequence[str],
        match: Literal["exact", "prefix"] = "exact",
        projection: Sequence[str] | None = None,
        symbol_kinds: Sequence[str] | None = None,
        paths: Sequence[str] | None = None,
        record_limit: int = 100,
        cursor: str | None = None,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> str:
        clean_names = _bounded_text_values(names, field="names", maximum=MAX_BATCH_SYMBOLS)
        if match not in {"exact", "prefix"}:
            raise QueryBoundaryError("Unsupported symbol match mode.", code="QUERY_INPUT_INVALID")
        clean_projection = _projection("symbols", projection)
        clean_kinds = _bounded_optional_values(symbol_kinds, field="symbol_kinds")
        clean_paths = _bounded_paths(paths, allow_empty=True) if paths is not None else []
        limit = _record_limit(record_limit)
        normalized = {
            "names": clean_names,
            "match": match,
            "projection": clean_projection,
            "symbol_kinds": clean_kinds,
            "paths": clean_paths,
            "record_limit": limit,
            "response_byte_limit": response_byte_limit,
        }
        identity = self._query_identity("find_symbols", normalized)
        offset = _decode_cursor(cursor, identity)

        def produce() -> _PageCandidate:
            clauses: list[str] = []
            values: list[Any] = []
            if match == "exact":
                placeholders = ",".join("?" for _ in clean_names)
                clauses.append(
                    f"(short_name IN ({placeholders}) OR qualified_name IN ({placeholders}) "
                    f"OR canonical_qualified_name IN ({placeholders}))"
                )
                values.extend([*clean_names, *clean_names, *clean_names])
            else:
                name_clauses: list[str] = []
                for name in clean_names:
                    name_clauses.append(
                        "(substr(short_name, 1, length(?)) = ? COLLATE BINARY "
                        "OR substr(qualified_name, 1, length(?)) = ? COLLATE BINARY "
                        "OR substr(COALESCE(canonical_qualified_name, ''), 1, length(?)) "
                        "= ? COLLATE BINARY)"
                    )
                    values.extend([name, name, name, name, name, name])
                clauses.append(f"({' OR '.join(name_clauses)})")
            if clean_kinds:
                placeholders = ",".join("?" for _ in clean_kinds)
                clauses.append(f"symbol_kind IN ({placeholders})")
                values.extend(clean_kinds)
            if clean_paths:
                keys = [path_key(item) for item in clean_paths]
                placeholders = ",".join("?" for _ in keys)
                clauses.append(f"path_key IN ({placeholders})")
                values.extend(keys)
            where = f"WHERE {' AND '.join(clauses)}"
            spec = DATASETS["symbols"]
            with self._connection() as conn:
                self._require_scan_bound(conn, spec)
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM ({spec.sql}) AS records {where}", values
                    ).fetchone()[0]
                )
                columns = ", ".join(_quoted(item) for item in clean_projection)
                order = ", ".join(_quoted(item) for item in spec.order_by)
                rows = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT {columns} FROM ({spec.sql}) AS records {where} "
                        f"ORDER BY {order} LIMIT ? OFFSET ?",
                        [*values, limit, offset],
                    ).fetchall()
                ]
            return _PageCandidate(
                item_key="records",
                items=rows,
                total=total,
                offset=offset,
                cursor_identity=identity,
                extra={
                    "query": {
                        "names": clean_names,
                        "match": match,
                        "projection": clean_projection,
                        "symbol_kinds": clean_kinds,
                        "paths": clean_paths,
                    }
                },
            )

        return self._run_call(
            "find_symbols",
            {**normalized, "cursor": cursor},
            response_byte_limit=response_byte_limit,
            producer=produce,
        )

    def inspect_paths(
        self,
        *,
        paths: Sequence[str],
        include: Sequence[str] | None = None,
        per_path_limit: int = 100,
        cursor: str | None = None,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> str:
        clean_paths = _bounded_paths(paths, allow_empty=False)
        allowed_sections = {"file", "symbols", "imports", "diagnostics"}
        clean_include = sorted(set(include or allowed_sections))
        if not clean_include or any(item not in allowed_sections for item in clean_include):
            raise QueryBoundaryError(
                "inspect include values are invalid.", code="QUERY_INPUT_INVALID"
            )
        limit = _record_limit(per_path_limit)
        normalized = {
            "paths": clean_paths,
            "include": clean_include,
            "per_path_limit": limit,
            "response_byte_limit": response_byte_limit,
        }
        identity = self._query_identity("inspect_paths", normalized)
        offset = _decode_cursor(cursor, identity)
        if offset > len(clean_paths):
            raise QueryBoundaryError(
                "Cursor offset is outside the result set.", code="CURSOR_INVALID"
            )

        def produce() -> _PageCandidate:
            inspections: list[dict[str, Any]] = []
            with self._connection() as conn:
                for relative_path in clean_paths[offset:]:
                    inspections.append(
                        self._inspect_one_path(
                            conn,
                            relative_path,
                            include=clean_include,
                            per_path_limit=limit,
                        )
                    )
            return _PageCandidate(
                item_key="paths",
                items=inspections,
                total=len(clean_paths),
                offset=offset,
                cursor_identity=identity,
                extra={
                    "query": {
                        "paths": clean_paths,
                        "include": clean_include,
                        "per_path_limit": limit,
                    }
                },
                records_counter=_inspection_record_count,
            )

        return self._run_call(
            "inspect_paths",
            {**normalized, "cursor": cursor},
            response_byte_limit=response_byte_limit,
            producer=produce,
        )

    def query_structural_records(
        self,
        *,
        dataset: DatasetName,
        operation: OperationName = "rows",
        filters: Sequence[Mapping[str, Any]] | None = None,
        projection: Sequence[str] | None = None,
        distinct_field: str | None = None,
        group_by: Sequence[str] | None = None,
        lookup_field: str | None = None,
        lookup_values: Sequence[str | int | bool] | None = None,
        record_limit: int = 100,
        cursor: str | None = None,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> str:
        if dataset not in DATASETS:
            raise QueryBoundaryError("Dataset is not allowlisted.", code="QUERY_INPUT_INVALID")
        if operation not in {"rows", "count", "distinct", "group_count", "batch_lookup"}:
            raise QueryBoundaryError("Operation is not allowlisted.", code="QUERY_INPUT_INVALID")
        spec = DATASETS[dataset]
        clean_filters = _normalize_filters(filters or (), spec)
        clean_projection = _projection(dataset, projection)
        clean_group_by = list(group_by or ())
        limit = _record_limit(record_limit)
        if operation == "distinct":
            if distinct_field not in spec.columns:
                raise QueryBoundaryError(
                    "distinct_field is not allowlisted for the dataset.",
                    code="QUERY_INPUT_INVALID",
                )
        elif distinct_field is not None:
            raise QueryBoundaryError(
                "distinct_field is valid only for distinct.", code="QUERY_INPUT_INVALID"
            )
        if operation == "group_count":
            if (
                not clean_group_by
                or len(clean_group_by) > MAX_GROUP_FIELDS
                or len(set(clean_group_by)) != len(clean_group_by)
                or any(item not in spec.columns for item in clean_group_by)
            ):
                raise QueryBoundaryError(
                    f"group_by requires 1-{MAX_GROUP_FIELDS} allowlisted fields.",
                    code="QUERY_INPUT_INVALID",
                )
        elif clean_group_by:
            raise QueryBoundaryError(
                "group_by is valid only for group_count.", code="QUERY_INPUT_INVALID"
            )
        if operation == "batch_lookup":
            if lookup_field not in spec.columns:
                raise QueryBoundaryError(
                    "lookup_field is not allowlisted for the dataset.",
                    code="QUERY_INPUT_INVALID",
                )
            clean_lookup_values = _bounded_scalar_values(
                lookup_values or (), field="lookup_values", maximum=MAX_FILTER_VALUES
            )
            clean_filters.append({"field": lookup_field, "op": "in", "value": clean_lookup_values})
            clean_filters = sorted(clean_filters, key=_canonical_json)
        else:
            if lookup_field is not None or lookup_values is not None:
                raise QueryBoundaryError(
                    "lookup inputs are valid only for batch_lookup.", code="QUERY_INPUT_INVALID"
                )
            clean_lookup_values = []
        normalized = {
            "dataset": dataset,
            "operation": operation,
            "filters": clean_filters,
            "projection": clean_projection,
            "distinct_field": distinct_field,
            "group_by": clean_group_by,
            "lookup_field": lookup_field,
            "lookup_values": clean_lookup_values,
            "record_limit": limit,
            "response_byte_limit": response_byte_limit,
        }
        identity = self._query_identity("query_structural_records", normalized)
        offset = _decode_cursor(cursor, identity)

        def produce() -> _PageCandidate:
            where, values = _where_clause(clean_filters)
            with self._connection() as conn:
                self._require_scan_bound(conn, spec)
                if operation == "count":
                    count = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM ({spec.sql}) AS records {where}", values
                        ).fetchone()[0]
                    )
                    return _PageCandidate(
                        item_key="records",
                        items=[{"count": count}],
                        total=count,
                        offset=0,
                        cursor_identity=None,
                        extra={"dataset": dataset, "operation": operation},
                        paginate=False,
                    )
                if operation == "distinct":
                    assert distinct_field is not None
                    field = _quoted(distinct_field)
                    grouped = f"SELECT DISTINCT {field} FROM ({spec.sql}) AS records {where}"
                    total = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM ({grouped}) AS groups", values
                        ).fetchone()[0]
                    )
                    rows = [
                        dict(row)
                        for row in conn.execute(
                            f"{grouped} ORDER BY {field} LIMIT ? OFFSET ?",
                            [*values, limit, offset],
                        ).fetchall()
                    ]
                elif operation == "group_count":
                    fields = ", ".join(_quoted(item) for item in clean_group_by)
                    grouped = (
                        f"SELECT {fields}, COUNT(*) AS count FROM ({spec.sql}) AS records {where} "
                        f"GROUP BY {fields}"
                    )
                    total = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM ({grouped}) AS groups", values
                        ).fetchone()[0]
                    )
                    rows = [
                        dict(row)
                        for row in conn.execute(
                            f"{grouped} ORDER BY {fields} LIMIT ? OFFSET ?",
                            [*values, limit, offset],
                        ).fetchall()
                    ]
                else:
                    total = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM ({spec.sql}) AS records {where}", values
                        ).fetchone()[0]
                    )
                    columns = ", ".join(_quoted(item) for item in clean_projection)
                    order = ", ".join(_quoted(item) for item in spec.order_by)
                    rows = [
                        dict(row)
                        for row in conn.execute(
                            f"SELECT {columns} FROM ({spec.sql}) AS records {where} "
                            f"ORDER BY {order} LIMIT ? OFFSET ?",
                            [*values, limit, offset],
                        ).fetchall()
                    ]
            return _PageCandidate(
                item_key="records",
                items=rows,
                total=total,
                offset=offset,
                cursor_identity=identity,
                extra={
                    "dataset": dataset,
                    "operation": operation,
                    "query": {
                        "filters": clean_filters,
                        "projection": clean_projection,
                        "distinct_field": distinct_field,
                        "group_by": clean_group_by,
                        "lookup_field": lookup_field,
                        "lookup_values": clean_lookup_values,
                    },
                },
            )

        return self._run_call(
            "query_structural_records",
            {**normalized, "cursor": cursor},
            response_byte_limit=response_byte_limit,
            producer=produce,
            fixed_returned=1 if operation == "count" else None,
        )

    def _inspect_one_path(
        self,
        conn: sqlite3.Connection,
        relative_path: str,
        *,
        include: Sequence[str],
        per_path_limit: int,
    ) -> dict[str, Any]:
        key = path_key(relative_path)
        file_row = conn.execute("SELECT * FROM files WHERE path_key = ?", [key]).fetchone()
        if file_row is None:
            return {
                "relative_path": relative_path,
                "found": False,
                "returned": 0,
                "total": 0,
                "truncated": False,
            }
        result: dict[str, Any] = {"relative_path": relative_path, "found": True}
        returned = 0
        total = 0
        truncated = False
        if "file" in include:
            result["file"] = dict(file_row)
            returned += 1
            total += 1
        sections = {
            "symbols": (DATASETS["symbols"], "path_key = ?"),
            "imports": (DATASETS["imports"], "path_key = ?"),
            "diagnostics": (DATASETS["parse_diagnostics"], "path_key = ?"),
        }
        for section, (spec, where) in sections.items():
            if section not in include:
                continue
            section_total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM ({spec.sql}) AS records WHERE {where}", [key]
                ).fetchone()[0]
            )
            order = ", ".join(_quoted(item) for item in spec.order_by)
            rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM ({spec.sql}) AS records WHERE {where} ORDER BY {order} LIMIT ?",
                    [key, per_path_limit],
                ).fetchall()
            ]
            result[section] = rows
            result[f"{section}_returned"] = len(rows)
            result[f"{section}_total"] = section_total
            returned += len(rows)
            total += section_total
            truncated = truncated or len(rows) < section_total
        result.update({"returned": returned, "total": total, "truncated": truncated})
        return result

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        uri = f"{self.config.path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            progress_calls = 0
            interval = 1_000
            maximum_calls = max(1, self.max_vm_steps // interval)

            def progress() -> int:
                nonlocal progress_calls
                progress_calls += 1
                return int(progress_calls > maximum_calls)

            conn.set_progress_handler(progress, interval)
            row = conn.execute("SELECT snapshot_id FROM snapshot_meta").fetchone()
            if row is None or row["snapshot_id"] != self.config.snapshot_id:
                raise QueryBoundaryError(
                    "Pinned snapshot identity changed during query setup.",
                    code="FROZEN_SNAPSHOT_CHANGED",
                )
            try:
                yield conn
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    raise QueryBoundaryError(
                        "Structural query exceeded the bounded work limit.",
                        code="QUERY_WORK_LIMIT_EXCEEDED",
                    ) from exc
                raise

    def _require_scan_bound(self, conn: sqlite3.Connection, spec: _DatasetSpec) -> None:
        total = int(conn.execute(f"SELECT COUNT(*) FROM ({spec.sql}) AS records").fetchone()[0])
        if total > self.max_scan_records:
            raise QueryBoundaryError(
                "Structural dataset exceeds the configured scan bound.",
                code="QUERY_SCAN_LIMIT_EXCEEDED",
            )

    def _query_identity(self, tool: str, normalized: Mapping[str, Any]) -> str:
        payload = {"source_ref": self.source_ref, "tool": tool, "query": normalized}
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def _run_call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        response_byte_limit: int,
        producer: Callable[[], _PageCandidate],
        fixed_returned: int | None = None,
    ) -> str:
        started = time.perf_counter_ns()
        self._call_count += 1
        call_id = self._call_count
        request_bytes = len(_canonical_bytes({"tool": tool, "arguments": arguments}))
        effective_limit = (
            response_byte_limit
            if isinstance(response_byte_limit, int)
            and not isinstance(response_byte_limit, bool)
            and MIN_RESPONSE_BYTE_LIMIT <= response_byte_limit <= MAX_RESPONSE_BYTE_LIMIT
            else DEFAULT_RESPONSE_BYTE_LIMIT
        )
        try:
            if effective_limit != response_byte_limit:
                raise QueryBoundaryError(
                    f"response_byte_limit must be between {MIN_RESPONSE_BYTE_LIMIT} "
                    f"and {MAX_RESPONSE_BYTE_LIMIT}.",
                    code="RESPONSE_BYTE_LIMIT_INVALID",
                )
            candidate = producer()
            ended = time.perf_counter_ns()
            return self._fit_success_response(
                tool,
                candidate,
                call_id=call_id,
                started=started,
                ended=ended,
                request_bytes=request_bytes,
                response_byte_limit=effective_limit,
                cursor_used=arguments.get("cursor") is not None,
                fixed_returned=fixed_returned,
            )
        except (QueryBoundaryError, SnapshotQueryError, OSError, sqlite3.Error, ValueError) as exc:
            ended = time.perf_counter_ns()
            code = getattr(exc, "code", "STRUCTURAL_QUERY_FAILED")
            return self._error_response(
                tool,
                code=str(code),
                message=str(exc),
                call_id=call_id,
                started=started,
                ended=ended,
                request_bytes=request_bytes,
                response_byte_limit=effective_limit,
            )

    def _fit_success_response(
        self,
        tool: str,
        candidate: _PageCandidate,
        *,
        call_id: int,
        started: int,
        ended: int,
        request_bytes: int,
        response_byte_limit: int,
        cursor_used: bool,
        fixed_returned: int | None,
    ) -> str:
        item_counts = range(len(candidate.items), -1, -1) if candidate.item_key else range(1)
        for item_count in item_counts:
            selected = candidate.items[:item_count]
            returned = fixed_returned if fixed_returned is not None else len(selected)
            if candidate.paginate:
                consumed = candidate.offset + len(selected)
                truncated = consumed < candidate.total
                next_cursor = (
                    _encode_cursor(consumed, candidate.cursor_identity)
                    if truncated and candidate.cursor_identity is not None
                    else None
                )
            else:
                truncated = False
                next_cursor = None
            payload: dict[str, Any] = {
                "ok": True,
                "tool": tool,
                "returned": returned,
                "total": candidate.total,
                "truncated": truncated,
                "next_cursor": next_cursor,
                "response_bytes": 0,
                "source_ref": self.source_ref,
                **candidate.extra,
            }
            if candidate.item_key is not None:
                payload[candidate.item_key] = selected
            payload["telemetry"] = self._telemetry(
                tool,
                call_id=call_id,
                started=started,
                ended=ended,
                request_bytes=request_bytes,
                records_returned=candidate.records_counter(selected),
                cursor_used=cursor_used,
                truncated=truncated,
                error=False,
            )
            encoded = _stabilize_response_bytes(payload)
            if len(encoded) <= response_byte_limit:
                if (
                    candidate.item_key is not None
                    and not selected
                    and candidate.offset < candidate.total
                ):
                    break
                return encoded.decode("utf-8")
        raise QueryBoundaryError(
            "No complete record fits inside the response byte limit.",
            code="RESPONSE_BYTE_LIMIT_TOO_SMALL",
        )

    def _error_response(
        self,
        tool: str,
        *,
        code: str,
        message: str,
        call_id: int,
        started: int,
        ended: int,
        request_bytes: int,
        response_byte_limit: int,
    ) -> str:
        payload: dict[str, Any] = {
            "ok": False,
            "tool": tool,
            "returned": 0,
            "total": 0,
            "truncated": False,
            "next_cursor": None,
            "response_bytes": 0,
            "source_ref": self.source_ref,
            "error": {"code": code, "message": message},
            "telemetry": self._telemetry(
                tool,
                call_id=call_id,
                started=started,
                ended=ended,
                request_bytes=request_bytes,
                records_returned=0,
                cursor_used=False,
                truncated=False,
                error=True,
            ),
        }
        encoded = _stabilize_response_bytes(payload)
        if len(encoded) > response_byte_limit:
            payload["error"]["message"] = "Bounded structural query failed."
            encoded = _stabilize_response_bytes(payload)
        return encoded.decode("utf-8")

    def _telemetry(
        self,
        tool: str,
        *,
        call_id: int,
        started: int,
        ended: int,
        request_bytes: int,
        records_returned: int,
        cursor_used: bool,
        truncated: bool,
        error: bool,
    ) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "operation": tool,
            "server_id": self.server_id,
            "process_id": self.process_id,
            "process_start_count": self.process_start_count,
            "bootstrap_count": self.bootstrap_count,
            "snapshot_validation_count": self.snapshot_validation_count,
            "server_started_monotonic_ns": self.server_started_monotonic_ns,
            "call_started_monotonic_ns": started,
            "call_ended_monotonic_ns": ended,
            "latency_ms": round((ended - started) / 1_000_000, 3),
            "request_bytes": request_bytes,
            "response_bytes": 0,
            "records_returned": records_returned,
            "pages": 1,
            "cursor_used": cursor_used,
            "truncated": truncated,
            "error": error,
            "retry_count": 0,
        }


def _normalize_filters(
    filters: Sequence[Mapping[str, Any]], spec: _DatasetSpec
) -> list[dict[str, Any]]:
    if len(filters) > MAX_FILTERS:
        raise QueryBoundaryError(
            f"At most {MAX_FILTERS} filters are allowed.", code="QUERY_INPUT_INVALID"
        )
    normalized: list[dict[str, Any]] = []
    for raw in filters:
        field = raw.get("field")
        operation = raw.get("op", "eq")
        value = raw.get("value")
        if field not in spec.columns or operation not in {"eq", "prefix", "in", "is_null"}:
            raise QueryBoundaryError("Filter is not allowlisted.", code="QUERY_INPUT_INVALID")
        if operation == "is_null":
            if value not in {None, True, False}:
                raise QueryBoundaryError(
                    "is_null accepts only a boolean value.", code="QUERY_INPUT_INVALID"
                )
            value = True if value is None else value
        elif operation == "in":
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise QueryBoundaryError(
                    "in filter requires a bounded value list.", code="QUERY_INPUT_INVALID"
                )
            value = _bounded_scalar_values(value, field="filter values", maximum=MAX_FILTER_VALUES)
        else:
            value = _bounded_scalar(value, field="filter value")
            if operation == "prefix" and not isinstance(value, str):
                raise QueryBoundaryError("prefix filter requires text.", code="QUERY_INPUT_INVALID")
        normalized.append({"field": field, "op": operation, "value": value})
    return sorted(normalized, key=_canonical_json)


def _where_clause(filters: Sequence[Mapping[str, Any]]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    for item in filters:
        field = _quoted(str(item["field"]))
        operation = item["op"]
        value = item["value"]
        if operation == "eq":
            clauses.append(f"{field} = ?")
            values.append(value)
        elif operation == "prefix":
            clauses.append(f"substr({field}, 1, length(?)) = ? COLLATE BINARY")
            values.extend([value, value])
        elif operation == "in":
            placeholders = ",".join("?" for _ in value)
            clauses.append(f"{field} IN ({placeholders})")
            values.extend(value)
        else:
            clauses.append(f"{field} IS {'NULL' if value else 'NOT NULL'}")
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", values)


def _projection(dataset: str, projection: Sequence[str] | None) -> list[str]:
    spec = DATASETS[dataset]
    selected = list(projection or spec.columns)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(item not in spec.columns for item in selected)
    ):
        raise QueryBoundaryError(
            "Projection contains non-allowlisted or duplicate fields.", code="QUERY_INPUT_INVALID"
        )
    return selected


def _record_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_RECORD_LIMIT:
        raise QueryBoundaryError(
            f"Record limit must be between 1 and {MAX_RECORD_LIMIT}.",
            code="QUERY_INPUT_INVALID",
        )
    return value


def _bounded_text_values(values: Sequence[str], *, field: str, maximum: int) -> list[str]:
    clean = _bounded_scalar_values(values, field=field, maximum=maximum)
    if any(not isinstance(item, str) or not item for item in clean):
        raise QueryBoundaryError(
            f"{field} requires non-empty text values.", code="QUERY_INPUT_INVALID"
        )
    return sorted(set(clean))


def _bounded_optional_values(values: Sequence[str] | None, *, field: str) -> list[str]:
    return _bounded_text_values(values, field=field, maximum=MAX_FILTER_VALUES) if values else []


def _bounded_paths(values: Sequence[str], *, allow_empty: bool) -> list[str]:
    if isinstance(values, (str, bytes)) or len(values) > MAX_PATHS:
        raise QueryBoundaryError(
            f"paths must contain at most {MAX_PATHS} path values.",
            code="QUERY_INPUT_INVALID",
        )
    if not values and not allow_empty:
        raise QueryBoundaryError(
            f"paths must contain between 1 and {MAX_PATHS} path values.",
            code="QUERY_INPUT_INVALID",
        )
    return sorted({_safe_relative_path(item) for item in values})


def _bounded_scalar_values(
    values: Sequence[str | int | bool], *, field: str, maximum: int
) -> list[str | int | bool]:
    if not values or len(values) > maximum:
        raise QueryBoundaryError(
            f"{field} must contain between 1 and {maximum} values.", code="QUERY_INPUT_INVALID"
        )
    clean = [_bounded_scalar(item, field=field) for item in values]
    return sorted(set(clean), key=lambda item: (type(item).__name__, str(item)))


def _bounded_scalar(value: Any, *, field: str) -> str | int | bool:
    if not isinstance(value, (str, int, bool)) or (isinstance(value, str) and len(value) > 512):
        raise QueryBoundaryError(
            f"{field} contains an unsupported value.", code="QUERY_INPUT_INVALID"
        )
    return value


def _safe_relative_path(raw: str) -> str:
    if not isinstance(raw, str):
        raise QueryBoundaryError(
            "Path must be repository-relative text.", code="QUERY_INPUT_INVALID"
        )
    if len(raw) > MAX_PATH_LENGTH:
        raise QueryBoundaryError(
            f"Path must not exceed {MAX_PATH_LENGTH} characters.",
            code="QUERY_INPUT_INVALID",
        )
    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw.replace("\\", "/"))
    if (
        windows_path.drive
        or windows_path.is_absolute()
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise QueryBoundaryError(
            "Path must be safe and repository-relative.", code="QUERY_INPUT_INVALID"
        )
    try:
        normalized = normalize_relative_path(raw)
    except ValueError as exc:
        raise QueryBoundaryError(
            "Path must be safe and repository-relative.", code="QUERY_INPUT_INVALID"
        ) from exc
    if not normalized or normalized == ".":
        raise QueryBoundaryError(
            "Path must identify one repository-relative file.", code="QUERY_INPUT_INVALID"
        )
    return normalized


def _quoted(identifier: str) -> str:
    return f'"{identifier}"'


def _encode_cursor(offset: int, identity: str | None) -> str:
    if identity is None:
        raise ValueError("cursor identity is required")
    payload = {"version": CURSOR_VERSION, "offset": offset, "identity": identity}
    checksum = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    envelope = {"payload": payload, "checksum": checksum}
    encoded = base64.urlsafe_b64encode(_canonical_bytes(envelope)).rstrip(b"=")
    return encoded.decode("ascii")


def _decode_cursor(cursor: str | None, identity: str) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        payload = envelope["payload"]
        checksum = envelope["checksum"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueryBoundaryError("Paging cursor is malformed.", code="CURSOR_INVALID") from exc
    expected = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    if checksum != expected or payload.get("version") != CURSOR_VERSION:
        raise QueryBoundaryError("Paging cursor is malformed.", code="CURSOR_INVALID")
    if payload.get("identity") != identity:
        raise QueryBoundaryError(
            "Paging cursor does not belong to this source and normalized query.",
            code="CURSOR_SOURCE_OR_QUERY_MISMATCH",
        )
    offset = payload.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise QueryBoundaryError("Paging cursor offset is invalid.", code="CURSOR_INVALID")
    return offset


def _inspection_record_count(items: Sequence[dict[str, Any]]) -> int:
    return sum(int(item.get("returned", 0)) for item in items)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QueryBoundaryError(
            "Frozen snapshot artifact is unavailable.", code="FROZEN_SNAPSHOT_UNAVAILABLE"
        ) from exc
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_bytes(value: Any) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _stabilize_response_bytes(payload: dict[str, Any]) -> bytes:
    for _ in range(12):
        encoded = _canonical_bytes(payload)
        size = len(encoded)
        if payload["response_bytes"] == size and payload["telemetry"]["response_bytes"] == size:
            return encoded
        payload["response_bytes"] = size
        payload["telemetry"]["response_bytes"] = size
    raise RuntimeError("response byte size did not stabilize")
