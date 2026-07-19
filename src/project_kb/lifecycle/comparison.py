"""Deterministic semantic-v2 comparison for exact lifecycle generations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from project_kb.errors import LifecycleOperationError
from project_kb.lifecycle.selector import (
    LifecycleSelectorRequest,
    LifecycleSelectorService,
    ResolvedGenerationDescriptor,
)
from project_kb.registry.db import open_existing_registry
from project_kb.storage.home import resolve_home

COMPARISON_REQUEST_VERSION = 1
COMPARISON_CONTRACT_VERSION = 1
CURSOR_VERSION = 1

_INCOMPARABLE_REASONS = {
    "CROSS_PROJECT_COMPARISON_FORBIDDEN": "Cross-project comparison is forbidden.",
    "CAPTURE_CONTRACT_MISMATCH": "Capture contracts differ.",
    "SEMANTIC_SCHEMA_INCOMPATIBLE": (
        "Semantic snapshot schemas or protected comparison contracts are incompatible."
    ),
    "REQUIRED_DIMENSION_UNAVAILABLE": ("A required protected comparison dimension is unavailable."),
}


class ComparisonMode(StrEnum):
    DIFF = "DIFF"
    ASSERT_EQUIVALENT = "ASSERT_EQUIVALENT"


class ComparisonStatus(StrEnum):
    COMPARABLE = "COMPARABLE"
    INCOMPARABLE = "INCOMPARABLE"


class ChangeKind(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class LineageRelation(StrEnum):
    SAME_GENERATION = "SAME_GENERATION"
    LEFT_ANCESTOR_OF_RIGHT = "LEFT_ANCESTOR_OF_RIGHT"
    RIGHT_ANCESTOR_OF_LEFT = "RIGHT_ANCESTOR_OF_LEFT"
    SAME_TASK_NOT_ANCESTRAL = "SAME_TASK_NOT_ANCESTRAL"
    UNRELATED = "UNRELATED"


ALL_DIMENSIONS = (
    "metadata",
    "files",
    "modules",
    "pruned_roots",
    "symbols",
    "imports",
    "relations",
    "diagnostics",
    "proof_semantics",
    "run_summary",
)


LifecycleReference = LifecycleSelectorRequest | ResolvedGenerationDescriptor


@dataclass(frozen=True)
class ComparisonLimits:
    max_page_size: int = 1_000
    max_rows: int = 5_000_000
    max_rows_per_logical_key: int = 10_000
    max_assertion_changes: int = 100_000
    max_detail_values: int = 100_000
    max_detail_bytes: int = 10 * 1024 * 1024
    max_filter_count: int = 100
    max_filter_length: int = 1_024
    max_cursor_length: int = 4_096
    max_lineage_depth: int = 100_000


@dataclass(frozen=True)
class ComparisonRequest:
    left: LifecycleReference
    right: LifecycleReference
    mode: ComparisonMode | str
    version: int = COMPARISON_REQUEST_VERSION
    comparison_contract_version: int = COMPARISON_CONTRACT_VERSION
    dimensions: tuple[str, ...] = ALL_DIMENSIONS
    path_prefixes: tuple[str, ...] = ()
    module_prefixes: tuple[str, ...] = ()
    change_kinds: tuple[ChangeKind | str, ...] = ()
    page_size: int = 100
    cursor: str | None = None


@dataclass(frozen=True)
class _NormalizedFilters:
    path_prefixes: tuple[str, ...]
    module_prefixes: tuple[str, ...]
    change_kinds: frozenset[ChangeKind]

    def to_dict(self) -> dict[str, tuple[str, ...]]:
        return {
            "path_prefixes": self.path_prefixes,
            "module_prefixes": self.module_prefixes,
            "change_kinds": tuple(sorted(kind.value for kind in self.change_kinds)),
        }


@dataclass(frozen=True)
class ChangeRecord:
    dimension: str
    kind: ChangeKind
    logical_key: Mapping[str, Any]
    left_values: tuple[Mapping[str, Any], ...]
    right_values: tuple[Mapping[str, Any], ...]
    left_instances: int
    right_instances: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "kind": self.kind.value,
            "logical_key": dict(self.logical_key),
            "left_values": [dict(value) for value in self.left_values],
            "right_values": [dict(value) for value in self.right_values],
            "left_instances": self.left_instances,
            "right_instances": self.right_instances,
        }


@dataclass(frozen=True)
class DimensionSummary:
    dimension: str
    added_groups: int = 0
    removed_groups: int = 0
    modified_groups: int = 0
    left_instances: int = 0
    right_instances: int = 0

    @property
    def changed_groups(self) -> int:
        return self.added_groups + self.removed_groups + self.modified_groups

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "changed_groups": self.changed_groups,
            "added_groups": self.added_groups,
            "removed_groups": self.removed_groups,
            "modified_groups": self.modified_groups,
            "left_instances": self.left_instances,
            "right_instances": self.right_instances,
        }


@dataclass(frozen=True)
class ComparisonResult:
    comparison_id: str
    request_version: int
    comparison_contract_version: int
    direction: str
    mode: ComparisonMode
    status: ComparisonStatus
    outcome: str
    equivalent: bool | None
    reason_code: str | None
    reason: str
    protected_dimensions: tuple[str, ...]
    normalized_filters: Mapping[str, Any]
    differing_dimensions: tuple[str, ...]
    unavailable_dimensions: tuple[str, ...]
    lineage: LineageRelation | None
    left: ResolvedGenerationDescriptor
    right: ResolvedGenerationDescriptor
    screening_fingerprints_equal: bool | None
    summaries: tuple[DimensionSummary, ...] = ()
    changes: tuple[ChangeRecord, ...] = ()
    total_changed_groups: int = 0
    next_cursor: str | None = None
    page_offset: int = 0
    page_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "request_version": self.request_version,
            "comparison_contract_version": self.comparison_contract_version,
            "direction": self.direction,
            "mode": self.mode.value,
            "status": self.status.value,
            "outcome": self.outcome,
            "equivalent": self.equivalent,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "protected_dimensions": list(self.protected_dimensions),
            "normalized_filters": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in self.normalized_filters.items()
            },
            "differing_dimensions": list(self.differing_dimensions),
            "unavailable_dimensions": list(self.unavailable_dimensions),
            "lineage": self.lineage.value if self.lineage is not None else None,
            "left": _stable_descriptor_dict(self.left),
            "right": _stable_descriptor_dict(self.right),
            "screening_fingerprints_equal": self.screening_fingerprints_equal,
            "summaries": [summary.to_dict() for summary in self.summaries],
            "changes": [change.to_dict() for change in self.changes],
            "total_changed_groups": self.total_changed_groups,
            "next_cursor": self.next_cursor,
            "pagination": (
                {
                    "offset": self.page_offset,
                    "page_size": self.page_size,
                    "returned_changed_groups": len(self.changes),
                    "truncated": self.next_cursor is not None,
                    "next_cursor": self.next_cursor,
                }
                if self.mode is ComparisonMode.DIFF
                else None
            ),
        }


@dataclass(frozen=True)
class QueryPlan:
    dimension: str
    details: tuple[str, ...]


@dataclass(frozen=True)
class _DimensionSpec:
    name: str
    key_names: tuple[str, ...]
    sql: str
    key_columns: tuple[str, ...]
    payload_columns: tuple[str, ...]
    json_payload_columns: tuple[str, ...] = ()
    path_filter_fields: tuple[str, ...] = ()
    module_filter_fields: tuple[str, ...] = ()

    def project(self, row: sqlite3.Row) -> tuple[tuple[Any, ...], str]:
        key = tuple(row[column] for column in self.key_columns)
        payload: dict[str, Any] = {}
        for column in self.payload_columns:
            value = row[column]
            if column in self.json_payload_columns:
                try:
                    value = json.loads(value)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise LifecycleOperationError(
                        "Protected comparison JSON is invalid.",
                        code="COMPARISON_DIMENSION_UNSUPPORTED",
                        details={"dimension": self.name, "column": column},
                    ) from exc
            payload[column] = value
        return key, _canonical_json(payload)


@dataclass
class _ScanBudget:
    limit: int
    rows: int = 0

    def consume(self) -> None:
        self.rows += 1
        if self.rows > self.limit:
            raise LifecycleOperationError(
                "Comparison row budget was exceeded.",
                code="COMPARISON_RESOURCE_LIMIT",
                details={"max_rows": self.limit},
            )


@dataclass
class _MutableSummary:
    dimension: str
    added_groups: int = 0
    removed_groups: int = 0
    modified_groups: int = 0
    left_instances: int = 0
    right_instances: int = 0

    def add(self, record: ChangeRecord) -> None:
        if record.kind is ChangeKind.ADDED:
            self.added_groups += 1
        elif record.kind is ChangeKind.REMOVED:
            self.removed_groups += 1
        else:
            self.modified_groups += 1
        self.left_instances += record.left_instances
        self.right_instances += record.right_instances

    def freeze(self) -> DimensionSummary:
        return DimensionSummary(
            dimension=self.dimension,
            added_groups=self.added_groups,
            removed_groups=self.removed_groups,
            modified_groups=self.modified_groups,
            left_instances=self.left_instances,
            right_instances=self.right_instances,
        )


_DIMENSIONS = (
    _DimensionSpec(
        "metadata",
        ("scope",),
        """SELECT 'snapshot' AS scope, schema_version, scanner_version,
                  policy_version, policy_json, extractor_versions_json,
                  build_status, proof_contract_version, verifier_version,
                  module_map_version, occurrence_contract_version,
                  verification_scope_json
           FROM snapshot_meta ORDER BY scope""",
        ("scope",),
        (
            "schema_version",
            "scanner_version",
            "policy_version",
            "policy_json",
            "extractor_versions_json",
            "build_status",
            "proof_contract_version",
            "verifier_version",
            "module_map_version",
            "occurrence_contract_version",
            "verification_scope_json",
        ),
        ("policy_json", "extractor_versions_json", "verification_scope_json"),
    ),
    _DimensionSpec(
        "files",
        ("path_key",),
        """SELECT path_key, relative_path, file_kind, language, extension,
                  line_count, encoding, content_hash, analysis_level,
                  classification_reason, parse_status
           FROM files
           ORDER BY path_key, relative_path, file_kind, language, extension,
                    line_count, encoding, content_hash, analysis_level,
                    classification_reason, parse_status""",
        ("path_key",),
        (
            "relative_path",
            "file_kind",
            "language",
            "extension",
            "line_count",
            "encoding",
            "content_hash",
            "analysis_level",
            "classification_reason",
            "parse_status",
        ),
        path_filter_fields=("path_key", "relative_path"),
    ),
    _DimensionSpec(
        "modules",
        ("module_key_version", "module_key_kind", "module_comparison_key"),
        """SELECT 1 AS module_key_version,
                  CASE WHEN module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS module_key_kind,
                  CASE WHEN module_resolution_status = 'EXACT'
                       THEN module_name ELSE path_key END AS module_comparison_key,
                  path_key, relative_path, source_root_path, source_root_origin,
                  module_name, module_resolution_status, module_candidates_json,
                  is_importable
           FROM files
           ORDER BY module_key_version, module_key_kind, module_comparison_key,
                    path_key, relative_path, source_root_path, source_root_origin,
                    module_name, module_resolution_status, module_candidates_json,
                    is_importable""",
        ("module_key_version", "module_key_kind", "module_comparison_key"),
        (
            "path_key",
            "relative_path",
            "source_root_path",
            "source_root_origin",
            "module_name",
            "module_resolution_status",
            "module_candidates_json",
            "is_importable",
        ),
        ("module_candidates_json",),
        path_filter_fields=("path_key", "relative_path"),
        module_filter_fields=(
            "module_comparison_key",
            "module_name",
            "module_candidates_json",
        ),
    ),
    _DimensionSpec(
        "pruned_roots",
        ("relative_path",),
        """SELECT relative_path, category, reason, source_policy
           FROM pruned_roots
           ORDER BY relative_path, category, reason, source_policy""",
        ("relative_path",),
        ("category", "reason", "source_policy"),
        path_filter_fields=("relative_path",),
    ),
    _DimensionSpec(
        "symbols",
        (
            "module_key_version",
            "module_key_kind",
            "module_comparison_key",
            "qualified_identity",
            "symbol_kind",
        ),
        """SELECT 1 AS module_key_version,
                  CASE WHEN f.module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS module_key_kind,
                  CASE WHEN f.module_resolution_status = 'EXACT'
                       THEN f.module_name ELSE f.path_key END AS module_comparison_key,
                  CASE WHEN f.module_resolution_status = 'EXACT'
                       THEN s.canonical_qualified_name
                       ELSE s.qualified_name END AS qualified_identity,
                  s.symbol_kind,
                  f.path_key,
                  f.relative_path, s.qualified_name, s.canonical_qualified_name,
                  s.module_name, s.short_name, s.symbol_kind, s.binding_role,
                  s.start_line, s.end_line, s.start_column, s.end_column,
                  CASE WHEN parent.symbol_id IS NULL THEN NULL
                       WHEN f.module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS parent_module_key_kind,
                  CASE WHEN parent.symbol_id IS NULL THEN NULL
                       WHEN f.module_resolution_status = 'EXACT'
                       THEN f.module_name ELSE f.path_key
                       END AS parent_module_comparison_key,
                  CASE WHEN parent.symbol_id IS NULL THEN NULL
                       WHEN f.module_resolution_status = 'EXACT'
                       THEN parent.canonical_qualified_name
                       ELSE parent.qualified_name END AS parent_qualified_identity,
                  parent.symbol_kind AS parent_symbol_kind,
                  s.signature_text, s.occurrence_ordinal,
                  s.occurrence_contract_version, s.extractor_name,
                  s.extractor_version
           FROM symbols s JOIN files f ON f.file_id = s.file_id
           LEFT JOIN symbols parent ON parent.symbol_id = s.parent_symbol_id
           ORDER BY module_key_version, module_key_kind, module_comparison_key,
                    qualified_identity, s.symbol_kind, f.path_key, f.relative_path,
                    s.qualified_name, s.canonical_qualified_name, s.module_name,
                    s.short_name, s.binding_role, s.start_line,
                    s.end_line, s.start_column, s.end_column,
                    parent_module_key_kind, parent_module_comparison_key,
                    parent_qualified_identity, parent_symbol_kind, s.signature_text,
                    s.occurrence_ordinal, s.occurrence_contract_version,
                    s.extractor_name, s.extractor_version""",
        (
            "module_key_version",
            "module_key_kind",
            "module_comparison_key",
            "qualified_identity",
            "symbol_kind",
        ),
        (
            "path_key",
            "relative_path",
            "qualified_name",
            "canonical_qualified_name",
            "module_name",
            "short_name",
            "symbol_kind",
            "binding_role",
            "start_line",
            "end_line",
            "start_column",
            "end_column",
            "parent_module_key_kind",
            "parent_module_comparison_key",
            "parent_qualified_identity",
            "parent_symbol_kind",
            "signature_text",
            "occurrence_ordinal",
            "occurrence_contract_version",
            "extractor_name",
            "extractor_version",
        ),
        path_filter_fields=("path_key", "relative_path"),
        module_filter_fields=("module_comparison_key", "module_name"),
    ),
    _DimensionSpec(
        "imports",
        (
            "source_module_key_kind",
            "source_module_comparison_key",
            "import_kind",
            "target_module_key_kind",
            "target_module_comparison_key",
            "imported_name",
            "alias",
            "relative_level",
        ),
        """SELECT CASE WHEN f.module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS source_module_key_kind,
                  CASE WHEN f.module_resolution_status = 'EXACT'
                       THEN f.module_name ELSE f.path_key
                       END AS source_module_comparison_key,
                  i.import_kind,
                  CASE WHEN v2_target.file_id IS NULL THEN 'MODULE_TEXT'
                       WHEN v2_target.module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS target_module_key_kind,
                  CASE WHEN v2_target.file_id IS NULL
                       THEN COALESCE(i.normalized_module_name, i.module_text)
                       WHEN v2_target.module_resolution_status = 'EXACT'
                       THEN v2_target.module_name ELSE v2_target.path_key
                       END AS target_module_comparison_key,
                  i.imported_name, i.alias, i.relative_level,
                  f.path_key AS source_path_key, f.relative_path,
                  i.module_text, i.start_line, i.end_line, i.resolution_status,
                  target.path_key AS resolved_path_key,
                  i.normalized_module_name, i.v2_resolution_status,
                  v2_target.path_key AS v2_resolved_path_key,
                  i.extractor_name, i.extractor_version
           FROM imports i
           JOIN files f ON f.file_id = i.file_id
           LEFT JOIN files target ON target.file_id = i.resolved_file_id
           LEFT JOIN files v2_target ON v2_target.file_id = i.v2_resolved_file_id
           ORDER BY source_module_key_kind, source_module_comparison_key,
                    i.import_kind, target_module_key_kind,
                    target_module_comparison_key, i.imported_name, i.alias,
                    i.relative_level, source_path_key, f.relative_path,
                    i.module_text, i.start_line, i.end_line,
                    i.resolution_status, resolved_path_key,
                    i.normalized_module_name, i.v2_resolution_status,
                    v2_resolved_path_key, i.extractor_name, i.extractor_version""",
        (
            "source_module_key_kind",
            "source_module_comparison_key",
            "import_kind",
            "target_module_key_kind",
            "target_module_comparison_key",
            "imported_name",
            "alias",
            "relative_level",
        ),
        (
            "source_path_key",
            "relative_path",
            "module_text",
            "start_line",
            "end_line",
            "resolution_status",
            "resolved_path_key",
            "normalized_module_name",
            "v2_resolution_status",
            "v2_resolved_path_key",
            "extractor_name",
            "extractor_version",
        ),
        path_filter_fields=("source_path_key", "relative_path", "resolved_path_key"),
        module_filter_fields=(
            "source_module_comparison_key",
            "target_module_comparison_key",
            "normalized_module_name",
            "module_text",
        ),
    ),
    _DimensionSpec(
        "relations",
        (
            "relation_kind",
            "source_module_key_kind",
            "source_module_comparison_key",
            "source_qualified_identity",
            "source_symbol_kind",
            "target_module_key_kind",
            "target_module_comparison_key",
            "target_qualified_identity",
            "target_symbol_kind",
            "target_text",
        ),
        """SELECT r.relation_kind,
                  CASE WHEN sf.file_id IS NULL THEN NULL
                       WHEN sf.module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS source_module_key_kind,
                  CASE WHEN sf.file_id IS NULL THEN NULL
                       WHEN sf.module_resolution_status = 'EXACT'
                       THEN sf.module_name ELSE sf.path_key
                       END AS source_module_comparison_key,
                  CASE WHEN ss.symbol_id IS NULL THEN NULL
                       WHEN sf.module_resolution_status = 'EXACT'
                       THEN ss.canonical_qualified_name
                       ELSE ss.qualified_name END AS source_qualified_identity,
                  ss.symbol_kind AS source_symbol_kind,
                  CASE WHEN tf.file_id IS NULL THEN NULL
                       WHEN tf.module_resolution_status = 'EXACT'
                       THEN 'EXACT' ELSE 'PATH' END AS target_module_key_kind,
                  CASE WHEN tf.file_id IS NULL THEN NULL
                       WHEN tf.module_resolution_status = 'EXACT'
                       THEN tf.module_name ELSE tf.path_key
                       END AS target_module_comparison_key,
                  CASE WHEN ts.symbol_id IS NULL THEN NULL
                       WHEN tf.module_resolution_status = 'EXACT'
                       THEN ts.canonical_qualified_name
                       ELSE ts.qualified_name END AS target_qualified_identity,
                  ts.symbol_kind AS target_symbol_kind,
                  r.target_text, sf.path_key AS source_path_key,
                  sf.relative_path AS source_relative_path,
                  tf.path_key AS target_path_key,
                  tf.relative_path AS target_relative_path,
                  r.start_line, r.end_line, r.start_column, r.end_column,
                  r.resolution_status, r.evidence_kind,
                  r.extractor_name, r.extractor_version
           FROM relations r
           LEFT JOIN files sf ON sf.file_id = r.source_file_id
           LEFT JOIN symbols ss ON ss.symbol_id = r.source_symbol_id
           LEFT JOIN files tf ON tf.file_id = r.target_file_id
           LEFT JOIN symbols ts ON ts.symbol_id = r.target_symbol_id
           ORDER BY r.relation_kind, source_module_key_kind,
                    source_module_comparison_key, source_qualified_identity,
                    source_symbol_kind, target_module_key_kind,
                    target_module_comparison_key, target_qualified_identity,
                    target_symbol_kind, r.target_text, source_path_key,
                    source_relative_path, target_path_key, target_relative_path,
                    r.start_line, r.end_line, r.start_column, r.end_column,
                    r.resolution_status, r.evidence_kind, r.extractor_name,
                    r.extractor_version""",
        (
            "relation_kind",
            "source_module_key_kind",
            "source_module_comparison_key",
            "source_qualified_identity",
            "source_symbol_kind",
            "target_module_key_kind",
            "target_module_comparison_key",
            "target_qualified_identity",
            "target_symbol_kind",
            "target_text",
        ),
        (
            "source_path_key",
            "source_relative_path",
            "target_path_key",
            "target_relative_path",
            "start_line",
            "end_line",
            "start_column",
            "end_column",
            "resolution_status",
            "evidence_kind",
            "extractor_name",
            "extractor_version",
        ),
        path_filter_fields=(
            "source_path_key",
            "source_relative_path",
            "target_path_key",
            "target_relative_path",
        ),
        module_filter_fields=(
            "source_module_comparison_key",
            "target_module_comparison_key",
        ),
    ),
    _DimensionSpec(
        "diagnostics",
        ("path_key", "diagnostic_kind", "message", "line", "column"),
        """SELECT f.path_key, d.diagnostic_kind, d.message, d.line, d.column,
                  f.relative_path, d.extractor_name, d.extractor_version
           FROM parse_diagnostics d JOIN files f ON f.file_id = d.file_id
           ORDER BY f.path_key, d.diagnostic_kind, d.message, d.line, d.column,
                    f.relative_path, d.extractor_name, d.extractor_version""",
        ("path_key", "diagnostic_kind", "message", "line", "column"),
        ("relative_path", "extractor_name", "extractor_version"),
        path_filter_fields=("path_key", "relative_path"),
    ),
    _DimensionSpec(
        "proof_semantics",
        ("path_key",),
        """SELECT path_key, relative_path, proof_class, evidence_kind,
                  content_hash, verification_scope, exclusion_reason
           FROM proof_manifest
           ORDER BY path_key, relative_path, proof_class, evidence_kind,
                    content_hash, verification_scope, exclusion_reason""",
        ("path_key",),
        (
            "relative_path",
            "proof_class",
            "evidence_kind",
            "content_hash",
            "verification_scope",
            "exclusion_reason",
        ),
        path_filter_fields=("path_key", "relative_path"),
    ),
    _DimensionSpec(
        "run_summary",
        ("scope",),
        """SELECT 'run' AS scope, status, failure_code, candidate_count,
                  metadata_only_count, pruned_root_count, text_file_count,
                  parsed_file_count, parse_failure_count, symbol_count,
                  import_count, relation_count, warnings_json
           FROM index_runs ORDER BY scope""",
        ("scope",),
        (
            "status",
            "failure_code",
            "candidate_count",
            "metadata_only_count",
            "pruned_root_count",
            "text_file_count",
            "parsed_file_count",
            "parse_failure_count",
            "symbol_count",
            "import_count",
            "relation_count",
            "warnings_json",
        ),
        ("warnings_json",),
    ),
)

_SPEC_BY_NAME = {spec.name: spec for spec in _DIMENSIONS}


class LifecycleComparisonService:
    """Compare persisted protected dimensions without reading target files."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        limits: ComparisonLimits | None = None,
    ) -> None:
        self.home = (home or resolve_home()).resolve()
        self.selectors = LifecycleSelectorService(home=self.home)
        self.limits = limits or ComparisonLimits()

    def compare(self, request: ComparisonRequest) -> ComparisonResult:
        mode, dimensions, filters = self._validate_request(request)
        left = self.selectors.resolve_reference(request.left)
        right = self.selectors.resolve_reference(request.right)
        comparison_id = _comparison_id(
            request,
            mode=mode,
            dimensions=dimensions,
            filters=filters,
            left=left,
            right=right,
        )
        offset = _decode_cursor(request.cursor, comparison_id) if request.cursor else 0
        lineage = self._lineage(left, right)
        try:
            incompatibility = _incompatibility(left, right)
        except LifecycleOperationError as exc:
            if exc.code != "COMPARISON_DIMENSION_UNSUPPORTED":
                raise
            return self._incomparable_result(
                request=request,
                mode=mode,
                dimensions=dimensions,
                filters=filters,
                comparison_id=comparison_id,
                offset=offset,
                lineage=lineage,
                left=left,
                right=right,
                reason_code="REQUIRED_DIMENSION_UNAVAILABLE",
                unavailable_dimensions=dimensions,
            )
        if incompatibility is not None:
            unavailable = dimensions if incompatibility == "SEMANTIC_SCHEMA_INCOMPATIBLE" else ()
            return self._incomparable_result(
                request=request,
                mode=mode,
                dimensions=dimensions,
                filters=filters,
                comparison_id=comparison_id,
                offset=offset,
                lineage=lineage,
                left=left,
                right=right,
                reason_code=incompatibility,
                unavailable_dimensions=unavailable,
            )

        try:
            screening_equal = _logical_fingerprint(left.path) == _logical_fingerprint(right.path)
        except LifecycleOperationError as exc:
            if exc.code != "COMPARISON_DIMENSION_UNSUPPORTED":
                raise
            return self._incomparable_result(
                request=request,
                mode=mode,
                dimensions=dimensions,
                filters=filters,
                comparison_id=comparison_id,
                offset=offset,
                lineage=lineage,
                left=left,
                right=right,
                reason_code="REQUIRED_DIMENSION_UNAVAILABLE",
                unavailable_dimensions=("metadata",),
            )
        budget = _ScanBudget(self.limits.max_rows)
        summaries: list[DimensionSummary] = []
        page: list[ChangeRecord] = []
        detail_values = 0
        detail_bytes = 0
        eligible_count = 0
        for dimension in dimensions:
            spec = _SPEC_BY_NAME[dimension]
            summary = _MutableSummary(dimension)
            try:
                for record in _dimension_changes(
                    spec,
                    left.path,
                    right.path,
                    budget=budget,
                    max_group_rows=self.limits.max_rows_per_logical_key,
                ):
                    if not _matches_filters(spec, record, filters):
                        continue
                    summary.add(record)
                    append_record = False
                    if mode is ComparisonMode.ASSERT_EQUIVALENT:
                        if eligible_count >= self.limits.max_assertion_changes:
                            raise LifecycleOperationError(
                                "Assertion detail budget was exceeded.",
                                code="COMPARISON_RESOURCE_LIMIT",
                                details={
                                    "max_assertion_changes": (self.limits.max_assertion_changes)
                                },
                            )
                        append_record = True
                    elif offset <= eligible_count < offset + request.page_size:
                        append_record = True
                    if append_record:
                        detail_values += len(record.left_values) + len(record.right_values)
                        if detail_values > self.limits.max_detail_values:
                            raise LifecycleOperationError(
                                "Comparison detail-value budget was exceeded.",
                                code="COMPARISON_RESOURCE_LIMIT",
                                details={"max_detail_values": self.limits.max_detail_values},
                            )
                        detail_bytes += len(_canonical_json(record.to_dict()).encode("utf-8"))
                        if detail_bytes > self.limits.max_detail_bytes:
                            raise LifecycleOperationError(
                                "Comparison detail-byte budget was exceeded.",
                                code="COMPARISON_RESOURCE_LIMIT",
                                details={"max_detail_bytes": self.limits.max_detail_bytes},
                            )
                        page.append(record)
                    eligible_count += 1
            except LifecycleOperationError as exc:
                if exc.code != "COMPARISON_DIMENSION_UNSUPPORTED":
                    raise
                return self._incomparable_result(
                    request=request,
                    mode=mode,
                    dimensions=dimensions,
                    filters=filters,
                    comparison_id=comparison_id,
                    offset=offset,
                    lineage=lineage,
                    left=left,
                    right=right,
                    reason_code="REQUIRED_DIMENSION_UNAVAILABLE",
                    unavailable_dimensions=(dimension,),
                    screening_fingerprints_equal=screening_equal,
                )
            summaries.append(summary.freeze())

        next_cursor = None
        if mode is ComparisonMode.DIFF and offset + len(page) < eligible_count:
            next_cursor = _encode_cursor(comparison_id, offset + len(page))
        equivalent = eligible_count == 0
        frozen_summaries = tuple(summaries)
        return ComparisonResult(
            comparison_id=comparison_id,
            request_version=request.version,
            comparison_contract_version=request.comparison_contract_version,
            direction="LEFT_TO_RIGHT",
            mode=mode,
            status=ComparisonStatus.COMPARABLE,
            outcome="EQUIVALENT" if equivalent else "DIFFERENT",
            equivalent=equivalent,
            reason_code=None,
            reason=(
                "Protected dimensions are equivalent."
                if equivalent
                else "Protected dimensions differ."
            ),
            protected_dimensions=dimensions,
            normalized_filters=filters.to_dict(),
            differing_dimensions=tuple(
                summary.dimension for summary in frozen_summaries if summary.changed_groups
            ),
            unavailable_dimensions=(),
            lineage=lineage,
            left=left,
            right=right,
            screening_fingerprints_equal=screening_equal,
            summaries=frozen_summaries,
            changes=tuple(page),
            total_changed_groups=eligible_count,
            next_cursor=next_cursor,
            page_offset=offset,
            page_size=(request.page_size if mode is ComparisonMode.DIFF else None),
        )

    @staticmethod
    def _incomparable_result(
        *,
        request: ComparisonRequest,
        mode: ComparisonMode,
        dimensions: tuple[str, ...],
        filters: _NormalizedFilters,
        comparison_id: str,
        offset: int,
        lineage: LineageRelation,
        left: ResolvedGenerationDescriptor,
        right: ResolvedGenerationDescriptor,
        reason_code: str,
        unavailable_dimensions: tuple[str, ...],
        screening_fingerprints_equal: bool | None = None,
    ) -> ComparisonResult:
        return ComparisonResult(
            comparison_id=comparison_id,
            request_version=request.version,
            comparison_contract_version=request.comparison_contract_version,
            direction="LEFT_TO_RIGHT",
            mode=mode,
            status=ComparisonStatus.INCOMPARABLE,
            outcome="INCOMPARABLE",
            equivalent=None,
            reason_code=reason_code,
            reason=_INCOMPARABLE_REASONS[reason_code],
            protected_dimensions=dimensions,
            normalized_filters=filters.to_dict(),
            differing_dimensions=(),
            unavailable_dimensions=unavailable_dimensions,
            lineage=lineage,
            left=left,
            right=right,
            screening_fingerprints_equal=screening_fingerprints_equal,
            page_offset=offset,
            page_size=(request.page_size if mode is ComparisonMode.DIFF else None),
        )

    def inspect_query_plans(
        self,
        reference: LifecycleReference,
        *,
        dimensions: Sequence[str] = ALL_DIMENSIONS,
    ) -> tuple[QueryPlan, ...]:
        descriptor = self.selectors.resolve_reference(reference)
        selected = _validate_dimensions(tuple(dimensions))
        with _snapshot_connection(descriptor.path) as conn:
            return tuple(
                QueryPlan(
                    dimension=name,
                    details=tuple(
                        str(row[3])
                        for row in conn.execute(f"EXPLAIN QUERY PLAN {_SPEC_BY_NAME[name].sql}")
                    ),
                )
                for name in selected
            )

    def _validate_request(
        self,
        request: ComparisonRequest,
    ) -> tuple[ComparisonMode, tuple[str, ...], _NormalizedFilters]:
        if not isinstance(request, ComparisonRequest):
            raise LifecycleOperationError(
                "Comparison request has an invalid type.",
                code="COMPARISON_REQUEST_INVALID",
            )
        if (
            not isinstance(request.version, int)
            or isinstance(request.version, bool)
            or request.version != COMPARISON_REQUEST_VERSION
        ):
            raise LifecycleOperationError(
                "Comparison request version is unsupported.",
                code="COMPARISON_REQUEST_UNSUPPORTED",
                details={"version": request.version},
            )
        if (
            not isinstance(request.comparison_contract_version, int)
            or isinstance(request.comparison_contract_version, bool)
            or request.comparison_contract_version != COMPARISON_CONTRACT_VERSION
        ):
            raise LifecycleOperationError(
                "Comparison contract version is unsupported.",
                code="COMPARISON_CONTRACT_UNSUPPORTED",
                details={"version": request.comparison_contract_version},
            )
        if any(
            not isinstance(value, tuple)
            for value in (
                request.dimensions,
                request.path_prefixes,
                request.module_prefixes,
                request.change_kinds,
            )
        ) or (request.cursor is not None and not isinstance(request.cursor, str)):
            raise LifecycleOperationError(
                "Comparison sequence and cursor fields have invalid types.",
                code="COMPARISON_REQUEST_INVALID",
            )
        try:
            mode = ComparisonMode(request.mode)
            change_kinds = frozenset(ChangeKind(kind) for kind in request.change_kinds)
        except ValueError as exc:
            raise LifecycleOperationError(
                "Comparison mode or change filter is invalid.",
                code="COMPARISON_REQUEST_INVALID",
            ) from exc
        dimensions = _validate_dimensions(request.dimensions)
        if (
            isinstance(request.page_size, bool)
            or not isinstance(request.page_size, int)
            or request.page_size <= 0
            or request.page_size > self.limits.max_page_size
        ):
            raise LifecycleOperationError(
                "Comparison page size is outside the supported bounds.",
                code="COMPARISON_REQUEST_INVALID",
                details={"max_page_size": self.limits.max_page_size},
            )
        if any(not isinstance(value, str) or not value for value in request.path_prefixes):
            raise LifecycleOperationError(
                "Comparison path filters must be non-empty strings.",
                code="COMPARISON_REQUEST_INVALID",
            )
        if any(not isinstance(value, str) or not value for value in request.module_prefixes):
            raise LifecycleOperationError(
                "Comparison module filters must be non-empty strings.",
                code="COMPARISON_REQUEST_INVALID",
            )
        filters = _NormalizedFilters(
            path_prefixes=tuple(sorted(set(request.path_prefixes))),
            module_prefixes=tuple(sorted(set(request.module_prefixes))),
            change_kinds=change_kinds,
        )
        prefixes = filters.path_prefixes + filters.module_prefixes
        if len(prefixes) > self.limits.max_filter_count or any(
            len(value) > self.limits.max_filter_length for value in prefixes
        ):
            raise LifecycleOperationError(
                "Comparison filters exceed the supported bounds.",
                code="COMPARISON_REQUEST_INVALID",
                details={
                    "max_filter_count": self.limits.max_filter_count,
                    "max_filter_length": self.limits.max_filter_length,
                },
            )
        if request.cursor is not None and len(request.cursor) > self.limits.max_cursor_length:
            raise LifecycleOperationError(
                "Comparison cursor exceeds the supported bounds.",
                code="COMPARISON_CURSOR_INVALID",
                details={"max_cursor_length": self.limits.max_cursor_length},
            )
        if mode is ComparisonMode.ASSERT_EQUIVALENT and (
            dimensions != ALL_DIMENSIONS
            or filters.path_prefixes
            or filters.module_prefixes
            or filters.change_kinds
            or request.cursor is not None
        ):
            raise LifecycleOperationError(
                "Equivalence assertions must cover every protected dimension without filters.",
                code="PARTIAL_EQUIVALENCE_ASSERTION_FORBIDDEN",
            )
        return mode, dimensions, filters

    def _lineage(
        self,
        left: ResolvedGenerationDescriptor,
        right: ResolvedGenerationDescriptor,
    ) -> LineageRelation:
        if left.snapshot_id == right.snapshot_id:
            return LineageRelation.SAME_GENERATION
        with open_existing_registry(
            home=self.home,
            now=_utc_now,
            event_id=lambda: uuid.uuid4().hex,
        ) as conn:
            if conn is None:
                raise LifecycleOperationError(
                    "Lineage inspection requires an existing registry.",
                    code="REGISTRY_NOT_FOUND",
                )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return _lineage_relation(conn, left, right, self.limits.max_lineage_depth)


def _validate_dimensions(dimensions: tuple[str, ...]) -> tuple[str, ...]:
    if not dimensions or len(set(dimensions)) != len(dimensions):
        raise LifecycleOperationError(
            "Comparison dimensions must be a non-empty unique sequence.",
            code="COMPARISON_REQUEST_INVALID",
        )
    unknown = sorted(set(dimensions) - set(ALL_DIMENSIONS))
    if unknown:
        raise LifecycleOperationError(
            "Comparison dimension is unsupported.",
            code="COMPARISON_DIMENSION_UNSUPPORTED",
            details={"dimensions": unknown},
        )
    return tuple(name for name in ALL_DIMENSIONS if name in dimensions)


def _incompatibility(
    left: ResolvedGenerationDescriptor,
    right: ResolvedGenerationDescriptor,
) -> str | None:
    if left.project_id != right.project_id:
        return "CROSS_PROJECT_COMPARISON_FORBIDDEN"
    if left.capture_contract_fingerprint != right.capture_contract_fingerprint:
        return "CAPTURE_CONTRACT_MISMATCH"
    if left.semantic_schema_version != 2 or right.semantic_schema_version != 2:
        return "SEMANTIC_SCHEMA_INCOMPATIBLE"
    left_contract = _comparison_contract(left.path)
    right_contract = _comparison_contract(right.path)
    if left_contract != right_contract:
        return "SEMANTIC_SCHEMA_INCOMPATIBLE"
    return None


def _comparison_contract(path: Path) -> tuple[Any, ...]:
    with _snapshot_connection(path) as conn:
        row = conn.execute(
            """SELECT schema_version, proof_contract_version, module_map_version,
                      occurrence_contract_version
               FROM snapshot_meta"""
        ).fetchone()
    if row is None:
        raise LifecycleOperationError(
            "Snapshot comparison contract metadata is unavailable.",
            code="COMPARISON_DIMENSION_UNSUPPORTED",
        )
    return tuple(row)


def _logical_fingerprint(path: Path) -> str:
    with _snapshot_connection(path) as conn:
        row = conn.execute("SELECT logical_fingerprint FROM snapshot_meta").fetchone()
    if row is None or not isinstance(row[0], str):
        raise LifecycleOperationError(
            "Snapshot logical fingerprint is unavailable.",
            code="COMPARISON_DIMENSION_UNSUPPORTED",
        )
    return row[0]


def _dimension_changes(
    spec: _DimensionSpec,
    left_path: Path,
    right_path: Path,
    *,
    budget: _ScanBudget,
    max_group_rows: int,
) -> Iterator[ChangeRecord]:
    left_groups = _grouped_rows(
        spec,
        left_path,
        budget=budget,
        max_group_rows=max_group_rows,
    )
    right_groups = _grouped_rows(
        spec,
        right_path,
        budget=budget,
        max_group_rows=max_group_rows,
    )
    left_group = next(left_groups, None)
    right_group = next(right_groups, None)
    while left_group is not None or right_group is not None:
        if right_group is None or (
            left_group is not None and _sortable_key(left_group[0]) < _sortable_key(right_group[0])
        ):
            key, left_values = left_group
            right_values: Counter[str] = Counter()
            left_group = next(left_groups, None)
        elif left_group is None or _sortable_key(right_group[0]) < _sortable_key(left_group[0]):
            key, right_values = right_group
            left_values = Counter()
            right_group = next(right_groups, None)
        else:
            key, left_values = left_group
            _, right_values = right_group
            left_group = next(left_groups, None)
            right_group = next(right_groups, None)
        common = left_values & right_values
        left_residual = left_values - common
        right_residual = right_values - common
        if not left_residual and not right_residual:
            continue
        if left_residual and right_residual:
            kind = ChangeKind.MODIFIED
        elif left_residual:
            kind = ChangeKind.REMOVED
        else:
            kind = ChangeKind.ADDED
        yield ChangeRecord(
            dimension=spec.name,
            kind=kind,
            logical_key=dict(zip(spec.key_names, key, strict=True)),
            left_values=_counter_values(left_residual),
            right_values=_counter_values(right_residual),
            left_instances=sum(left_residual.values()),
            right_instances=sum(right_residual.values()),
        )


def _grouped_rows(
    spec: _DimensionSpec,
    path: Path,
    *,
    budget: _ScanBudget,
    max_group_rows: int,
) -> Iterator[tuple[tuple[Any, ...], Counter[str]]]:
    with _snapshot_connection(path) as conn:
        cursor = conn.execute(spec.sql)
        current_key: tuple[Any, ...] | None = None
        values: Counter[str] = Counter()
        group_rows = 0
        for row in cursor:
            budget.consume()
            key, payload = spec.project(row)
            if current_key is not None and key != current_key:
                yield current_key, values
                values = Counter()
                group_rows = 0
            current_key = key
            values[payload] += 1
            group_rows += 1
            if group_rows > max_group_rows:
                raise LifecycleOperationError(
                    "Comparison logical-key group budget was exceeded.",
                    code="COMPARISON_RESOURCE_LIMIT",
                    details={
                        "dimension": spec.name,
                        "max_rows_per_logical_key": max_group_rows,
                    },
                )
        if current_key is not None:
            yield current_key, values


def _matches_filters(
    spec: _DimensionSpec,
    record: ChangeRecord,
    filters: _NormalizedFilters,
) -> bool:
    if filters.change_kinds and record.kind not in filters.change_kinds:
        return False
    if filters.path_prefixes and not any(
        value.startswith(filters.path_prefixes)
        for value in _filter_values(record, spec.path_filter_fields)
    ):
        return False
    return not filters.module_prefixes or any(
        value.startswith(filters.module_prefixes)
        for value in _filter_values(record, spec.module_filter_fields)
    )


def _filter_values(
    record: ChangeRecord,
    field_names: tuple[str, ...],
) -> tuple[str, ...]:
    if not field_names:
        return ()
    sources: list[Mapping[str, Any]] = [record.logical_key]
    for occurrence in (*record.left_values, *record.right_values):
        value = occurrence.get("value")
        if isinstance(value, Mapping):
            sources.append(value)
    values: set[str] = set()
    for source in sources:
        for field_name in field_names:
            value = source.get(field_name)
            if isinstance(value, str):
                values.add(value)
            elif isinstance(value, (list, tuple)):
                values.update(item for item in value if isinstance(item, str))
    return tuple(sorted(values))


def _counter_values(counter: Counter[str]) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    for encoded in sorted(counter):
        values.append(
            {
                "value": json.loads(encoded),
                "multiplicity": counter[encoded],
            }
        )
    return tuple(values)


def _sortable_key(key: tuple[Any, ...]) -> tuple[tuple[bool, Any], ...]:
    return tuple((value is not None, value) for value in key)


def _comparison_id(
    request: ComparisonRequest,
    *,
    mode: ComparisonMode,
    dimensions: tuple[str, ...],
    filters: _NormalizedFilters,
    left: ResolvedGenerationDescriptor,
    right: ResolvedGenerationDescriptor,
) -> str:
    payload = {
        "request_version": request.version,
        "comparison_contract_version": request.comparison_contract_version,
        "mode": mode.value,
        "left_snapshot_id": left.snapshot_id,
        "right_snapshot_id": right.snapshot_id,
        "dimensions": list(dimensions),
        **{key: list(value) for key, value in filters.to_dict().items()},
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _encode_cursor(comparison_id: str, offset: int) -> str:
    body = {
        "version": CURSOR_VERSION,
        "comparison_id": comparison_id,
        "offset": offset,
    }
    checksum = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    envelope = {**body, "checksum": checksum}
    return (
        base64.urlsafe_b64encode(_canonical_json(envelope).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(cursor: str, comparison_id: str) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(f"{cursor}{padding}").decode("utf-8"))
    except (binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleOperationError(
            "Comparison cursor is malformed.",
            code="COMPARISON_CURSOR_INVALID",
        ) from exc
    if not isinstance(envelope, Mapping):
        raise LifecycleOperationError(
            "Comparison cursor is malformed.",
            code="COMPARISON_CURSOR_INVALID",
        )
    try:
        body = {
            "version": envelope["version"],
            "comparison_id": envelope["comparison_id"],
            "offset": envelope["offset"],
        }
        supplied_checksum = envelope["checksum"]
        checksum = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
        if (
            not isinstance(body["version"], int)
            or isinstance(body["version"], bool)
            or not isinstance(body["comparison_id"], str)
            or not isinstance(body["offset"], int)
            or isinstance(body["offset"], bool)
            or body["offset"] < 0
            or not isinstance(supplied_checksum, str)
            or not hmac.compare_digest(checksum, supplied_checksum)
        ):
            raise ValueError("cursor payload invalid")
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleOperationError(
            "Comparison cursor is malformed.",
            code="COMPARISON_CURSOR_INVALID",
        ) from exc
    if body["version"] != CURSOR_VERSION:
        raise LifecycleOperationError(
            "Comparison cursor version is unsupported.",
            code="COMPARISON_CURSOR_VERSION_UNSUPPORTED",
            details={"version": body["version"]},
        )
    if body["comparison_id"] != comparison_id:
        raise LifecycleOperationError(
            "Comparison cursor belongs to another normalized request.",
            code="COMPARISON_CURSOR_MISMATCH",
        )
    return body["offset"]


def _ancestor_ids(
    conn: sqlite3.Connection,
    descriptor: ResolvedGenerationDescriptor,
    max_depth: int,
) -> frozenset[str]:
    ancestors: set[str] = set()
    current = descriptor.snapshot_id
    child_sequence: int | None = None
    for _ in range(max_depth):
        row = conn.execute(
            """SELECT project_id, task_id, workspace_id,
                      workspace_binding_generation, generation_sequence,
                      parent_snapshot_id, capture_purpose, generation_state
               FROM snapshot_generations WHERE snapshot_id = ?""",
            (current,),
        ).fetchone()
        if row is None:
            raise LifecycleOperationError(
                "Generation lineage references a missing node.",
                code="GENERATION_LINEAGE_CORRUPT",
                details={"snapshot_id": current},
            )
        if (
            row["project_id"] != descriptor.project_id
            or row["task_id"] != descriptor.task_id
            or row["workspace_id"] != descriptor.workspace_id
            or row["workspace_binding_generation"] != descriptor.workspace_binding_generation
        ):
            raise LifecycleOperationError(
                "Generation lineage crosses an ownership boundary.",
                code="GENERATION_LINEAGE_CORRUPT",
                details={"snapshot_id": current},
            )
        sequence = row["generation_sequence"]
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or (current == descriptor.snapshot_id and sequence != descriptor.generation_sequence)
            or (child_sequence is not None and sequence >= child_sequence)
        ):
            raise LifecycleOperationError(
                "Generation lineage sequence is not strictly decreasing toward ancestors.",
                code="GENERATION_LINEAGE_CORRUPT",
                details={"snapshot_id": current},
            )
        parent = row["parent_snapshot_id"]
        purpose = row["capture_purpose"]
        if (
            (sequence == 0 and (purpose != "TASK_BASELINE" or parent is not None))
            or (sequence > 0 and (purpose not in {"TASK_WORKING", "TASK_FINAL"} or parent is None))
            or row["generation_state"]
            not in {"AVAILABLE", "ORPHANED", "QUARANTINED", "PENDING_DELETE", "DELETED"}
        ):
            raise LifecycleOperationError(
                "Generation lineage capture purpose or lifecycle state is contradictory.",
                code="GENERATION_LINEAGE_CORRUPT",
                details={"snapshot_id": current},
            )
        if parent is None:
            return frozenset(ancestors)
        if parent == current or parent in ancestors:
            raise LifecycleOperationError(
                "Generation lineage contains a cycle.",
                code="GENERATION_LINEAGE_CORRUPT",
                details={"snapshot_id": current},
            )
        ancestors.add(parent)
        child_sequence = sequence
        current = parent
    raise LifecycleOperationError(
        "Generation lineage exceeds the inspection depth limit.",
        code="COMPARISON_RESOURCE_LIMIT",
        details={"max_lineage_depth": max_depth},
    )


def _lineage_relation(
    conn: sqlite3.Connection,
    left: ResolvedGenerationDescriptor,
    right: ResolvedGenerationDescriptor,
    max_depth: int,
) -> LineageRelation:
    if left.snapshot_id == right.snapshot_id:
        return LineageRelation.SAME_GENERATION
    left_ancestors = _ancestor_ids(conn, left, max_depth)
    right_ancestors = _ancestor_ids(conn, right, max_depth)
    if left.task_id != right.task_id:
        return LineageRelation.UNRELATED
    if left.snapshot_id in right_ancestors:
        return LineageRelation.LEFT_ANCESTOR_OF_RIGHT
    if right.snapshot_id in left_ancestors:
        return LineageRelation.RIGHT_ANCESTOR_OF_LEFT
    return LineageRelation.SAME_TASK_NOT_ANCESTRAL


@contextmanager
def _snapshot_connection(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        uri = f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            yield conn
    except (OSError, sqlite3.Error) as exc:
        raise LifecycleOperationError(
            "Protected comparison dimension could not be read.",
            code="COMPARISON_DIMENSION_UNSUPPORTED",
            details={"error_type": type(exc).__name__},
        ) from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_descriptor_dict(
    descriptor: ResolvedGenerationDescriptor,
) -> dict[str, Any]:
    payload = descriptor.to_dict()
    payload.pop("resolution_timestamp", None)
    return payload


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
