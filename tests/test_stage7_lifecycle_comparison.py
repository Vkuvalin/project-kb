from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_stage7_lifecycle_selectors import (
    _build_lineage,
    _close_task,
    _commit_registration_marker,
    _exact_selector,
    _git,
    _init_repo,
    _task_selector,
)

from project_kb.errors import LifecycleOperationError
from project_kb.gating import GateRequirement
from project_kb.lifecycle.comparison import (
    _DIMENSIONS,
    _SPEC_BY_NAME,
    ALL_DIMENSIONS,
    ChangeKind,
    ComparisonLimits,
    ComparisonMode,
    ComparisonRequest,
    ComparisonStatus,
    LifecycleComparisonService,
    LineageRelation,
    _ancestor_ids,
    _dimension_changes,
    _DimensionSpec,
    _incompatibility,
    _lineage_relation,
    _matches_filters,
    _NormalizedFilters,
    _ScanBudget,
)
from project_kb.lifecycle.read import LifecycleReadService
from project_kb.lifecycle.selector import (
    LifecycleSelectorKind,
    LifecycleSelectorRequest,
)
from project_kb.lifecycle.task import (
    ActorContext,
    ActorKind,
    BeginTaskRequest,
    RefreshWorkingRequest,
    TaskLifecycleService,
)
from project_kb.registry import RegistryService
from project_kb.registry.db import registry_path
from project_kb.snapshot.currentness import CurrentnessState, VerificationMode


def test_equivalent_refreshes_are_byte_deterministic_and_lineage_is_directional(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    refreshed = TaskLifecycleService(home=lineage.home).refresh_working(
        ActorContext(ActorKind.CODEX, "test-codex", "refresh-equivalent"),
        RefreshWorkingRequest(
            idempotency_key="refresh-equivalent",
            task_context=lineage.contexts[-1],
            write_pause_acknowledged=True,
        ),
    )
    service = LifecycleComparisonService(home=lineage.home)
    request = ComparisonRequest(
        left=_exact_selector(lineage, refreshed.snapshot_id),
        right=_exact_selector(lineage, lineage.working_ids[0]),
        mode=ComparisonMode.ASSERT_EQUIVALENT,
    )
    first = service.compare(request)
    second = service.compare(request)

    assert first.status is ComparisonStatus.COMPARABLE
    assert first.direction == "LEFT_TO_RIGHT"
    assert first.outcome == "EQUIVALENT"
    assert first.equivalent
    assert first.total_changed_groups == 0
    assert first.lineage is LineageRelation.RIGHT_ANCESTOR_OF_LEFT
    assert first.screening_fingerprints_equal
    assert _bytes(first.to_dict()) == _bytes(second.to_dict())
    assert all(summary.changed_groups == 0 for summary in first.summaries)
    assert first.protected_dimensions == ALL_DIMENSIONS
    assert first.differing_dimensions == ()
    assert first.to_dict()["pagination"] is None

    reverse = service.compare(
        ComparisonRequest(
            left=request.right,
            right=request.left,
            mode=ComparisonMode.ASSERT_EQUIVALENT,
        )
    )
    assert reverse.lineage is LineageRelation.LEFT_ANCESTOR_OF_RIGHT
    assert reverse.comparison_id != first.comparison_id

    same = service.compare(replace(request, right=request.left))
    assert same.equivalent
    assert same.lineage is LineageRelation.SAME_GENERATION


def test_diff_paging_cursor_binding_and_full_assertion_rules(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ("changed",))
    service = LifecycleComparisonService(home=lineage.home)
    request = ComparisonRequest(
        left=_exact_selector(lineage, lineage.baseline_id),
        right=_exact_selector(lineage, lineage.working_ids[0]),
        mode=ComparisonMode.DIFF,
        page_size=1,
    )
    first = service.compare(request)
    assert first.status is ComparisonStatus.COMPARABLE
    assert first.outcome == "DIFFERENT"
    assert not first.equivalent
    assert first.total_changed_groups > 1
    assert len(first.changes) == 1
    assert first.next_cursor is not None
    assert first.differing_dimensions
    assert first.to_dict()["pagination"]["truncated"] is True

    second = service.compare(replace(request, page_size=2, cursor=first.next_cursor))
    assert second.comparison_id == first.comparison_id
    assert second.changes
    assert second.changes[0].to_dict() != first.changes[0].to_dict()

    seen: list[bytes] = []
    cursor: str | None = None
    cursor_sequence: list[str | None] = []
    while True:
        page = service.compare(replace(request, cursor=cursor))
        seen.extend(_bytes(change.to_dict()) for change in page.changes)
        cursor_sequence.append(page.next_cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(seen) == first.total_changed_groups
    assert len(set(seen)) == len(seen)

    repeated_cursors: list[str | None] = []
    cursor = None
    while True:
        page = service.compare(replace(request, cursor=cursor))
        repeated_cursors.append(page.next_cursor)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert repeated_cursors == cursor_sequence

    with pytest.raises(LifecycleOperationError) as cursor_error:
        service.compare(
            replace(
                request,
                path_prefixes=("module.py",),
                cursor=first.next_cursor,
            )
        )
    assert cursor_error.value.code == "COMPARISON_CURSOR_MISMATCH"

    with pytest.raises(LifecycleOperationError) as partial_assertion:
        service.compare(
            replace(
                request,
                mode=ComparisonMode.ASSERT_EQUIVALENT,
                dimensions=("files",),
                cursor=None,
            )
        )
    assert partial_assertion.value.code == "PARTIAL_EQUIVALENCE_ASSERTION_FORBIDDEN"

    with pytest.raises(LifecycleOperationError) as unsupported_request:
        service.compare(replace(request, version=99))
    assert unsupported_request.value.code == "COMPARISON_REQUEST_UNSUPPORTED"
    with pytest.raises(LifecycleOperationError) as unsupported_contract:
        service.compare(replace(request, comparison_contract_version=99))
    assert unsupported_contract.value.code == "COMPARISON_CONTRACT_UNSUPPORTED"

    with pytest.raises(LifecycleOperationError) as invalid_sequence:
        service.compare(replace(request, path_prefixes=["module.py"]))  # type: ignore[arg-type]
    assert invalid_sequence.value.code == "COMPARISON_REQUEST_INVALID"

    filtered = service.compare(
        replace(
            request,
            path_prefixes=("module.py",),
            change_kinds=(ChangeKind.ADDED,),
            page_size=100,
        )
    )
    assert filtered.changes
    assert all(change.kind is ChangeKind.ADDED for change in filtered.changes)
    assert all("module.py" in _bytes(change.to_dict()).decode() for change in filtered.changes)

    with pytest.raises(LifecycleOperationError) as detail_limited:
        LifecycleComparisonService(
            home=lineage.home,
            limits=ComparisonLimits(max_detail_values=1),
        ).compare(replace(request, page_size=100))
    assert detail_limited.value.code == "COMPARISON_RESOURCE_LIMIT"

    with pytest.raises(LifecycleOperationError) as byte_limited:
        LifecycleComparisonService(
            home=lineage.home,
            limits=ComparisonLimits(max_detail_bytes=1),
        ).compare(replace(request, page_size=100))
    assert byte_limited.value.code == "COMPARISON_RESOURCE_LIMIT"
    assert byte_limited.value.details == {"max_detail_bytes": 1}

    plans = service.inspect_query_plans(request.left)
    assert tuple(plan.dimension for plan in plans) == ALL_DIMENSIONS
    assert all(plan.details for plan in plans)
    assert any("INDEX" in detail.upper() for plan in plans for detail in plan.details)


def test_filter_normalization_and_cursor_failure_codes_are_exact(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ("changed",))
    service = LifecycleComparisonService(home=lineage.home)
    base = ComparisonRequest(
        left=_exact_selector(lineage, lineage.baseline_id),
        right=_exact_selector(lineage, lineage.working_ids[0]),
        mode=ComparisonMode.DIFF,
        path_prefixes=("module.py", "module.py", "unused/"),
        page_size=1,
    )
    normalized = replace(base, path_prefixes=("unused/", "module.py"))
    first = service.compare(base)
    second = service.compare(normalized)
    assert first.comparison_id == second.comparison_id
    assert first.next_cursor == second.next_cursor
    assert first.normalized_filters == second.normalized_filters
    assert first.normalized_filters["path_prefixes"] == ("module.py", "unused/")
    assert service.compare(replace(base, page_size=2)).comparison_id == first.comparison_id

    with pytest.raises(LifecycleOperationError) as malformed:
        service.compare(replace(base, cursor="not-a-cursor"))
    assert malformed.value.code == "COMPARISON_CURSOR_INVALID"

    assert first.next_cursor is not None
    with pytest.raises(LifecycleOperationError) as mismatched:
        service.compare(replace(base, module_prefixes=("different",), cursor=first.next_cursor))
    assert mismatched.value.code == "COMPARISON_CURSOR_MISMATCH"

    unsupported_cursor = _rewrite_cursor(first.next_cursor, version=99)
    with pytest.raises(LifecycleOperationError) as unsupported:
        service.compare(replace(base, cursor=unsupported_cursor))
    assert unsupported.value.code == "COMPARISON_CURSOR_VERSION_UNSUPPORTED"


@pytest.mark.parametrize(
    ("dimension", "mutation"),
    (
        ("metadata", "UPDATE snapshot_meta SET scanner_version = 'changed'"),
        ("files", "UPDATE files SET classification_reason = 'changed'"),
        ("modules", "UPDATE files SET module_resolution_status = 'changed'"),
        ("pruned_roots", "INSERT_PRUNED"),
        ("symbols", "UPDATE symbols SET signature_text = 'changed'"),
        ("imports", "UPDATE imports SET resolution_status = 'changed'"),
        ("relations", "UPDATE relations SET evidence_kind = 'changed'"),
        ("diagnostics", "INSERT_DIAGNOSTIC"),
        ("proof_semantics", "UPDATE proof_manifest SET exclusion_reason = 'changed'"),
        ("run_summary", "UPDATE index_runs SET warnings_json = '[{\"changed\":true}]'"),
    ),
)
def test_each_protected_dimension_has_an_isolated_deterministic_projection(
    tmp_path: Path,
    dimension: str,
    mutation: str,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    with sqlite3.connect(right) as conn:
        if mutation == "INSERT_PRUNED":
            snapshot_id = conn.execute("SELECT snapshot_id FROM snapshot_meta").fetchone()[0]
            conn.execute(
                """INSERT INTO pruned_roots (
                       snapshot_id, relative_path, category, reason, source_policy
                   ) VALUES (?, 'build/', 'BUILD', 'test', 'test')""",
                (snapshot_id,),
            )
        elif mutation == "INSERT_DIAGNOSTIC":
            snapshot_id, file_id = conn.execute(
                "SELECT snapshot_id, file_id FROM files ORDER BY path_key LIMIT 1"
            ).fetchone()
            conn.execute(
                """INSERT INTO parse_diagnostics (
                       snapshot_id, file_id, diagnostic_kind, message, line, column,
                       extractor_name, extractor_version
                   ) VALUES (?, ?, 'TEST', 'changed', 1, 1, 'test', '1')""",
                (snapshot_id, file_id),
            )
        else:
            cursor = conn.execute(mutation)
            assert cursor.rowcount > 0

    spec = next(item for item in _DIMENSIONS if item.name == dimension)
    first = list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100_000),
            max_group_rows=10_000,
        )
    )
    second = list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100_000),
            max_group_rows=10_000,
        )
    )
    assert first
    assert _bytes([record.to_dict() for record in first]) == _bytes(
        [record.to_dict() for record in second]
    )


def test_large_duplicate_groups_are_multisets_not_arbitrarily_paired(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    for path, changed_count in ((left, 0), (right, 1)):
        with sqlite3.connect(path) as conn:
            snapshot_id, file_id = conn.execute(
                "SELECT snapshot_id, file_id FROM files ORDER BY path_key LIMIT 1"
            ).fetchone()
            rows = [
                (
                    snapshot_id,
                    file_id,
                    "DUPLICATE",
                    "same logical diagnostic",
                    1,
                    1,
                    "test",
                    "2" if index < changed_count else "1",
                )
                for index in range(2_000)
            ]
            conn.executemany(
                """INSERT INTO parse_diagnostics (
                       snapshot_id, file_id, diagnostic_kind, message, line, column,
                       extractor_name, extractor_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            if path == right:
                conn.executemany(
                    """INSERT INTO parse_diagnostics (
                           snapshot_id, file_id, diagnostic_kind, message, line,
                           column, extractor_name, extractor_version
                       ) VALUES (?, ?, 'ADDED_DUPLICATE', 'added group', 1, 1,
                                 'test', '1')""",
                    ((snapshot_id, file_id) for _ in range(2_000)),
                )
    spec = next(item for item in _DIMENSIONS if item.name == "diagnostics")
    changes = list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(10_000),
            max_group_rows=3_000,
        )
    )
    duplicate_change = next(
        change for change in changes if change.logical_key["diagnostic_kind"] == "DUPLICATE"
    )
    assert duplicate_change.kind is ChangeKind.MODIFIED
    assert duplicate_change.left_instances == 1
    assert duplicate_change.right_instances == 1
    assert duplicate_change.left_values[0]["value"]["extractor_version"] == "1"
    assert duplicate_change.right_values[0]["value"]["extractor_version"] == "2"
    assert duplicate_change.left_values[0]["multiplicity"] == 1
    assert duplicate_change.right_values[0]["multiplicity"] == 1
    added_change = next(
        change for change in changes if change.logical_key["diagnostic_kind"] == "ADDED_DUPLICATE"
    )
    assert added_change.kind is ChangeKind.ADDED
    assert added_change.right_instances == 2_000
    assert len(added_change.right_values) == 1
    assert added_change.right_values[0]["multiplicity"] == 2_000

    with pytest.raises(LifecycleOperationError) as limited:
        list(
            _dimension_changes(
                spec,
                left,
                right,
                budget=_ScanBudget(10_000),
                max_group_rows=1_000,
            )
        )
    assert limited.value.code == "COMPARISON_RESOURCE_LIMIT"


def test_insertion_order_does_not_change_multiset_results(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    for path, values in ((left, range(300)), (right, reversed(range(300)))):
        with sqlite3.connect(path) as conn:
            snapshot_id, file_id = conn.execute(
                "SELECT snapshot_id, file_id FROM files ORDER BY path_key LIMIT 1"
            ).fetchone()
            conn.executemany(
                """INSERT INTO parse_diagnostics (
                       snapshot_id, file_id, diagnostic_kind, message, line, column,
                       extractor_name, extractor_version
                   ) VALUES (?, ?, 'ORDER', ?, 1, 1, 'test', '1')""",
                ((snapshot_id, file_id, f"message-{value:04d}") for value in values),
            )
    spec = next(item for item in _DIMENSIONS if item.name == "diagnostics")
    assert not list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(10_000),
            max_group_rows=1_000,
        )
    )


def test_json_member_order_is_canonicalized_before_projection(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    with sqlite3.connect(right) as conn:
        encoded = conn.execute("SELECT policy_json FROM snapshot_meta").fetchone()[0]
        payload = json.loads(encoded)
        conn.execute(
            "UPDATE snapshot_meta SET policy_json = ?",
            (json.dumps(dict(reversed(tuple(payload.items())))),),
        )
    spec = next(item for item in _DIMENSIONS if item.name == "metadata")
    assert not list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100),
            max_group_rows=10,
        )
    )


def test_ambiguous_module_candidates_use_explicit_module_filter_fields(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "ambiguous-filter-left.sqlite"
    right = tmp_path / "ambiguous-filter-right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    for path, candidates in (
        (left, ["other.module"]),
        (right, ["other.module", "pkg.candidate"]),
    ):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """UPDATE files
                   SET module_resolution_status = 'AMBIGUOUS', module_name = NULL,
                       module_candidates_json = ?, is_importable = 0
                   WHERE relative_path = 'module.py'""",
                (json.dumps(candidates),),
            )
    spec = _SPEC_BY_NAME["modules"]
    changes = list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100),
            max_group_rows=10,
        )
    )
    assert len(changes) == 1
    filters = _NormalizedFilters((), ("pkg",), frozenset())
    assert _matches_filters(spec, changes[0], filters)


@pytest.mark.parametrize(
    "dimension",
    ("files", "modules", "symbols", "imports", "relations"),
)
def test_protected_logical_duplicates_preserve_multiplicity_and_null_keys(
    tmp_path: Path,
    dimension: str,
) -> None:
    left = tmp_path / f"{dimension}-left.sqlite"
    right = tmp_path / f"{dimension}-right.sqlite"
    rows = [
        ("stable", "same"),
        ("stable", "same"),
        ("stable", "same"),
        (None, "null-one"),
        (None, "null-two"),
        ("ambiguous", "candidate-a"),
        ("ambiguous", "candidate-b"),
    ]
    for path, ordered_rows in ((left, rows), (right, list(reversed(rows)))):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE facts (logical_key TEXT, payload TEXT)")
            conn.executemany("INSERT INTO facts VALUES (?, ?)", ordered_rows)
    spec = _DimensionSpec(
        dimension,
        ("logical_key",),
        "SELECT logical_key, payload FROM facts ORDER BY logical_key, payload",
        ("logical_key",),
        ("payload",),
    )
    assert not list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100),
            max_group_rows=10,
        )
    )

    with sqlite3.connect(right) as conn:
        conn.execute(
            "DELETE FROM facts WHERE rowid = "
            "(SELECT rowid FROM facts WHERE logical_key = 'stable' LIMIT 1)"
        )
    changes = list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100),
            max_group_rows=10,
        )
    )
    assert len(changes) == 1
    assert changes[0].kind is ChangeKind.REMOVED
    assert changes[0].logical_key == {"logical_key": "stable"}
    assert changes[0].left_instances == 1
    assert changes[0].left_values[0]["multiplicity"] == 1


def test_path_change_is_removed_plus_added_even_with_identical_content_hash(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "rename-left.sqlite"
    right = tmp_path / "rename-right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    with sqlite3.connect(right) as conn:
        original_key, content_hash = conn.execute(
            "SELECT path_key, content_hash FROM files WHERE relative_path = 'module.py'"
        ).fetchone()
        conn.execute(
            "UPDATE files SET path_key = 'renamed.py', relative_path = 'renamed.py' "
            "WHERE path_key = ?",
            (original_key,),
        )
    spec = next(item for item in _DIMENSIONS if item.name == "files")
    changes = list(
        _dimension_changes(
            spec,
            left,
            right,
            budget=_ScanBudget(100),
            max_group_rows=10,
        )
    )
    assert [change.kind for change in changes] == [ChangeKind.REMOVED, ChangeKind.ADDED]
    assert [change.logical_key["path_key"] for change in changes] == [
        original_key,
        "renamed.py",
    ]
    assert changes[0].left_values[0]["value"]["content_hash"] == content_hash
    assert changes[1].right_values[0]["value"]["content_hash"] == content_hash


def test_module_keys_use_exact_identity_and_path_scoped_ambiguous_identity(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    exact_left = tmp_path / "module-exact-left.sqlite"
    exact_right = tmp_path / "module-exact-right.sqlite"
    shutil.copy2(descriptor.path, exact_left)
    shutil.copy2(descriptor.path, exact_right)
    for path in (exact_left, exact_right):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """UPDATE files SET module_resolution_status = 'EXACT',
                                    module_name = 'pkg.module', is_importable = 1
                   WHERE relative_path = 'module.py'"""
            )
    with sqlite3.connect(exact_right) as conn:
        conn.execute(
            """UPDATE files SET path_key = 'moved/module.py',
                                relative_path = 'moved/module.py'
               WHERE relative_path = 'module.py'"""
        )
    module_changes = _changes("modules", exact_left, exact_right)
    assert len(module_changes) == 1
    assert module_changes[0].kind is ChangeKind.MODIFIED
    assert module_changes[0].logical_key["module_key_kind"] == "EXACT"
    file_changes = _changes("files", exact_left, exact_right)
    assert [change.kind for change in file_changes] == [ChangeKind.REMOVED, ChangeKind.ADDED]

    ambiguous_left = tmp_path / "module-ambiguous-left.sqlite"
    ambiguous_right = tmp_path / "module-ambiguous-right.sqlite"
    shutil.copy2(descriptor.path, ambiguous_left)
    shutil.copy2(descriptor.path, ambiguous_right)
    for path in (ambiguous_left, ambiguous_right):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """UPDATE files
                   SET module_resolution_status = 'AMBIGUOUS', module_name = NULL,
                       module_candidates_json = '["pkg.one","pkg.two"]',
                       is_importable = 0
                   WHERE relative_path = 'module.py'"""
            )
    with sqlite3.connect(ambiguous_right) as conn:
        conn.execute(
            """UPDATE files SET path_key = 'moved/module.py',
                                relative_path = 'moved/module.py'
               WHERE relative_path = 'module.py'"""
        )
    ambiguous_changes = _changes("modules", ambiguous_left, ambiguous_right)
    assert [change.kind for change in ambiguous_changes] == [
        ChangeKind.REMOVED,
        ChangeKind.ADDED,
    ]
    assert all(change.logical_key["module_key_kind"] == "PATH" for change in ambiguous_changes)


def test_symbol_kind_and_parent_identity_are_part_of_stable_symbol_contract(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    kind_left = tmp_path / "symbol-kind-left.sqlite"
    kind_right = tmp_path / "symbol-kind-right.sqlite"
    shutil.copy2(descriptor.path, kind_left)
    shutil.copy2(descriptor.path, kind_right)
    with sqlite3.connect(kind_right) as conn:
        conn.execute("UPDATE symbols SET symbol_kind = 'CLASS' WHERE symbol_kind = 'FUNCTION'")
    kind_changes = [
        change
        for change in _changes("symbols", kind_left, kind_right)
        if str(change.logical_key["qualified_identity"]).endswith(".baseline")
    ]
    assert {change.kind for change in kind_changes} == {
        ChangeKind.REMOVED,
        ChangeKind.ADDED,
    }
    assert all(change.kind is not ChangeKind.MODIFIED for change in kind_changes)
    assert {change.logical_key["symbol_kind"] for change in kind_changes} == {
        "FUNCTION",
        "CLASS",
    }

    parent_left = tmp_path / "symbol-parent-left.sqlite"
    parent_right = tmp_path / "symbol-parent-right.sqlite"
    shutil.copy2(descriptor.path, parent_left)
    shutil.copy2(descriptor.path, parent_right)
    with sqlite3.connect(parent_right) as conn:
        conn.execute("UPDATE symbols SET symbol_kind = 'CLASS' WHERE symbol_kind = 'MODULE'")
    child_change = next(
        change
        for change in _changes("symbols", parent_left, parent_right)
        if str(change.logical_key["qualified_identity"]).endswith(".baseline")
    )
    assert child_change.kind is ChangeKind.MODIFIED
    assert child_change.left_values[0]["value"]["parent_symbol_kind"] == "MODULE"
    assert child_change.right_values[0]["value"]["parent_symbol_kind"] == "CLASS"

    duplicate_left = tmp_path / "symbol-ambiguous-left.sqlite"
    duplicate_right = tmp_path / "symbol-ambiguous-right.sqlite"
    shutil.copy2(descriptor.path, duplicate_left)
    shutil.copy2(descriptor.path, duplicate_right)
    for path in (duplicate_left, duplicate_right):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """UPDATE files
                   SET module_resolution_status = 'AMBIGUOUS', module_name = NULL,
                       module_candidates_json = '["pkg.one","pkg.two"]',
                       is_importable = 0
                   WHERE relative_path = 'module.py'"""
            )
            conn.execute(
                """UPDATE symbols
                   SET canonical_qualified_name = NULL, logical_key = NULL,
                       module_name = NULL"""
            )
            conn.execute(
                """INSERT INTO symbols (
                       symbol_id, legacy_symbol_id, snapshot_id, file_id,
                       qualified_name, canonical_qualified_name, logical_key,
                       module_name, short_name, symbol_kind, binding_role,
                       start_line, end_line, start_column, end_column,
                       parent_symbol_id, signature_text, occurrence_ordinal,
                       occurrence_contract_version, extractor_name, extractor_version
                   )
                   SELECT 'c' || substr(symbol_id, 2),
                          'c' || substr(legacy_symbol_id, 2), snapshot_id, file_id,
                          qualified_name, NULL, NULL, NULL, short_name, 'CLASS',
                          binding_role, start_line, end_line, start_column,
                          end_column, parent_symbol_id, signature_text,
                          occurrence_ordinal, occurrence_contract_version,
                          extractor_name, extractor_version
                   FROM symbols WHERE symbol_kind = 'FUNCTION'"""
            )
    with sqlite3.connect(duplicate_right) as conn:
        conn.execute("DELETE FROM symbols WHERE symbol_kind = 'CLASS'")
    duplicate_changes = [
        change
        for change in _changes("symbols", duplicate_left, duplicate_right)
        if str(change.logical_key["qualified_identity"]).endswith(".baseline")
    ]
    assert len(duplicate_changes) == 1
    assert duplicate_changes[0].kind is ChangeKind.REMOVED
    assert duplicate_changes[0].logical_key["symbol_kind"] == "CLASS"


def test_import_and_relation_edges_ignore_snapshot_ids_and_not_line_positions(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.baseline_id)
    )
    left = tmp_path / "edges-left.sqlite"
    right = tmp_path / "edges-right.sqlite"
    shutil.copy2(descriptor.path, left)
    shutil.copy2(descriptor.path, right)
    with sqlite3.connect(right) as conn:
        conn.execute("UPDATE imports SET import_id = 'rotated-' || import_id")
        conn.execute("UPDATE relations SET relation_id = 'rotated-' || relation_id")
    assert not _changes("imports", left, right)
    assert not _changes("relations", left, right)

    with sqlite3.connect(right) as conn:
        conn.execute("UPDATE imports SET start_line = start_line + 10, end_line = end_line + 10")
        conn.execute(
            """UPDATE relations
               SET start_line = start_line + 10, end_line = end_line + 10
               WHERE start_line IS NOT NULL"""
        )
    import_changes = _changes("imports", left, right)
    relation_changes = _changes("relations", left, right)
    assert import_changes and all(change.kind is ChangeKind.MODIFIED for change in import_changes)
    assert relation_changes and all(
        change.kind is ChangeKind.MODIFIED for change in relation_changes
    )


def test_unavailable_protected_dimension_returns_incomparable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    service = LifecycleComparisonService(home=lineage.home)
    monkeypatch.setitem(
        _SPEC_BY_NAME,
        "diagnostics",
        _DimensionSpec(
            "diagnostics",
            ("logical_key",),
            "SELECT logical_key, payload FROM unavailable_dimension",
            ("logical_key",),
            ("payload",),
        ),
    )
    result = service.compare(
        ComparisonRequest(
            left=_exact_selector(lineage, lineage.baseline_id),
            right=_exact_selector(lineage, lineage.baseline_id),
            mode=ComparisonMode.ASSERT_EQUIVALENT,
        )
    )
    assert result.status is ComparisonStatus.INCOMPARABLE
    assert result.reason_code == "REQUIRED_DIMENSION_UNAVAILABLE"
    assert result.reason == "A required protected comparison dimension is unavailable."
    assert result.unavailable_dimensions == ("diagnostics",)
    assert result.equivalent is None


def test_incomparable_reason_codes_are_separate_from_human_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineage = _build_lineage(tmp_path, ())
    service = LifecycleComparisonService(home=lineage.home)
    descriptor = service.selectors.resolve(_exact_selector(lineage, lineage.baseline_id))
    monkeypatch.setattr(service.selectors, "resolve_reference", lambda reference: reference)

    capture_mismatch = service.compare(
        ComparisonRequest(
            left=descriptor,
            right=replace(descriptor, capture_contract_fingerprint="0" * 64),
            mode=ComparisonMode.DIFF,
        )
    )
    assert capture_mismatch.status is ComparisonStatus.INCOMPARABLE
    assert capture_mismatch.reason_code == "CAPTURE_CONTRACT_MISMATCH"
    assert capture_mismatch.reason == "Capture contracts differ."

    schema_mismatch = service.compare(
        ComparisonRequest(
            left=descriptor,
            right=replace(descriptor, semantic_schema_version=1),
            mode=ComparisonMode.DIFF,
        )
    )
    assert schema_mismatch.status is ComparisonStatus.INCOMPARABLE
    assert schema_mismatch.reason_code == "SEMANTIC_SCHEMA_INCOMPATIBLE"
    assert "Semantic snapshot schemas" in schema_mismatch.reason


def test_cross_project_is_incomparable_and_lineage_cycles_fail_closed(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    first_root.mkdir()
    lineage = _build_lineage(first_root, ())
    with closing(sqlite3.connect(registry_path(lineage.home))) as conn:
        conn.execute(
            """UPDATE lifecycle_tasks
               SET task_state = 'ABANDONED', row_version = row_version + 1
               WHERE task_id = ?""",
            (lineage.task_id,),
        )
        conn.execute(
            """UPDATE lifecycle_tasks
               SET task_state = 'CLOSED', row_version = row_version + 1
               WHERE task_id = ?""",
            (lineage.task_id,),
        )
        conn.commit()
    same_project = TaskLifecycleService(home=lineage.home).begin_task(
        ActorContext(ActorKind.USER, "test-user", "same-project-begin"),
        BeginTaskRequest(
            idempotency_key="same-project-begin",
            project_id=lineage.project_id,
            workspace_id=lineage.workspace_id,
            workspace_binding_generation=lineage.binding_generation,
            expected_git_head=_git(lineage.repo, "rev-parse", "HEAD"),
            expected_head_ref=(f"refs/heads/{_git(lineage.repo, 'branch', '--show-current')}"),
        ),
    )
    service = LifecycleComparisonService(home=lineage.home)
    cross_task = service.compare(
        ComparisonRequest(
            left=_exact_selector(lineage, lineage.baseline_id),
            right=LifecycleSelectorRequest(
                version=1,
                kind=LifecycleSelectorKind.SNAPSHOT_ID,
                project_id=lineage.project_id,
                snapshot_id=same_project.snapshot_id,
            ),
            mode=ComparisonMode.ASSERT_EQUIVALENT,
        )
    )
    assert cross_task.status is ComparisonStatus.COMPARABLE
    assert cross_task.equivalent
    assert cross_task.lineage is LineageRelation.UNRELATED

    left_descriptor = service.selectors.resolve(_exact_selector(lineage, lineage.baseline_id))
    assert (
        _incompatibility(
            left_descriptor,
            replace(left_descriptor, capture_contract_fingerprint="0" * 64),
        )
        == "CAPTURE_CONTRACT_MISMATCH"
    )
    assert (
        _incompatibility(
            left_descriptor,
            replace(left_descriptor, semantic_schema_version=1),
        )
        == "SEMANTIC_SCHEMA_INCOMPATIBLE"
    )

    incompatible_contract = tmp_path / "incompatible-contract.sqlite"
    shutil.copy2(left_descriptor.path, incompatible_contract)
    with sqlite3.connect(incompatible_contract) as conn:
        conn.execute("UPDATE snapshot_meta SET module_map_version = 'changed'")
    assert (
        _incompatibility(
            left_descriptor,
            replace(left_descriptor, path=incompatible_contract),
        )
        == "SEMANTIC_SCHEMA_INCOMPATIBLE"
    )

    second_repo = tmp_path / "second-repo"
    _init_repo(second_repo, "second")
    project = RegistryService(home=lineage.home).register("repo-two", second_repo).project
    assert project is not None
    _commit_registration_marker(second_repo)
    second = TaskLifecycleService(home=lineage.home).begin_task(
        ActorContext(ActorKind.USER, "test-user", "second-begin"),
        BeginTaskRequest(
            idempotency_key="second-begin",
            project_id=project.project_id,
            workspace_id=project.workspace_id,
            workspace_binding_generation=project.repo_binding_generation,
            expected_git_head=_git(second_repo, "rev-parse", "HEAD"),
            expected_head_ref=(f"refs/heads/{_git(second_repo, 'branch', '--show-current')}"),
        ),
    )
    result = service.compare(
        ComparisonRequest(
            left=_exact_selector(lineage, lineage.baseline_id),
            right=LifecycleSelectorRequest(
                version=1,
                kind=LifecycleSelectorKind.SNAPSHOT_ID,
                project_id=project.project_id,
                snapshot_id=second.snapshot_id,
            ),
            mode=ComparisonMode.DIFF,
        )
    )
    assert result.status is ComparisonStatus.INCOMPARABLE
    assert result.reason_code == "CROSS_PROJECT_COMPARISON_FORBIDDEN"
    assert result.reason == "Cross-project comparison is forbidden."
    assert result.outcome == "INCOMPARABLE"
    assert result.equivalent is None

    descriptor = service.selectors.resolve(_exact_selector(lineage, lineage.baseline_id))
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE snapshot_generations (
                   snapshot_id TEXT PRIMARY KEY, project_id TEXT,
                   task_id TEXT, workspace_id TEXT,
                   workspace_binding_generation TEXT,
                   generation_sequence INTEGER, parent_snapshot_id TEXT,
                   capture_purpose TEXT, generation_state TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO snapshot_generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                descriptor.snapshot_id,
                descriptor.project_id,
                descriptor.task_id,
                descriptor.workspace_id,
                descriptor.workspace_binding_generation,
                descriptor.generation_sequence,
                descriptor.snapshot_id,
                descriptor.capture_purpose,
                descriptor.generation_state,
            ),
        )
        with pytest.raises(LifecycleOperationError) as cycle:
            _ancestor_ids(conn, descriptor, 10)
    assert cycle.value.code == "GENERATION_LINEAGE_CORRUPT"


def test_lineage_enforces_task_ownership_sequence_and_exact_relation_vocabulary(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    descriptor = LifecycleComparisonService(home=lineage.home).selectors.resolve(
        _exact_selector(lineage, lineage.working_ids[0])
    )
    baseline_id = lineage.baseline_id
    child_id = lineage.working_ids[0]
    sibling_id = "a" * 32
    other_task_id = "b" * 32
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE snapshot_generations (
                   snapshot_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
                   workspace_id TEXT, workspace_binding_generation TEXT,
                   generation_sequence INTEGER, parent_snapshot_id TEXT,
                   capture_purpose TEXT, generation_state TEXT
               )"""
        )

        def insert(
            snapshot_id: str,
            task_id: str,
            sequence: int,
            parent_snapshot_id: str | None,
        ) -> None:
            conn.execute(
                "INSERT INTO snapshot_generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    descriptor.project_id,
                    task_id,
                    descriptor.workspace_id,
                    descriptor.workspace_binding_generation,
                    sequence,
                    parent_snapshot_id,
                    "TASK_BASELINE" if sequence == 0 else "TASK_WORKING",
                    "AVAILABLE",
                ),
            )

        insert(baseline_id, descriptor.task_id, 0, None)
        insert(child_id, descriptor.task_id, 1, baseline_id)
        insert(sibling_id, descriptor.task_id, 2, baseline_id)
        left = replace(descriptor, snapshot_id=baseline_id, generation_sequence=0)
        child = replace(descriptor, snapshot_id=child_id, generation_sequence=1)
        sibling = replace(descriptor, snapshot_id=sibling_id, generation_sequence=2)
        assert _lineage_relation(conn, left, child, 10) is LineageRelation.LEFT_ANCESTOR_OF_RIGHT
        assert _lineage_relation(conn, child, left, 10) is LineageRelation.RIGHT_ANCESTOR_OF_LEFT
        assert (
            _lineage_relation(conn, child, sibling, 10) is LineageRelation.SAME_TASK_NOT_ANCESTRAL
        )

        conn.execute("DELETE FROM snapshot_generations")
        insert(child_id, descriptor.task_id, 1, baseline_id)
        insert(baseline_id, other_task_id, 0, None)
        with pytest.raises(LifecycleOperationError) as cross_task_edge:
            _ancestor_ids(conn, child, 10)
        assert cross_task_edge.value.code == "GENERATION_LINEAGE_CORRUPT"

        conn.execute("DELETE FROM snapshot_generations")
        insert(child_id, descriptor.task_id, 1, baseline_id)
        insert(baseline_id, descriptor.task_id, 2, None)
        with pytest.raises(LifecycleOperationError) as non_decreasing:
            _ancestor_ids(conn, child, 10)
        assert non_decreasing.value.code == "GENERATION_LINEAGE_CORRUPT"

        conn.execute("DELETE FROM snapshot_generations")
        insert(child_id, descriptor.task_id, 2, sibling_id)
        insert(sibling_id, descriptor.task_id, 1, child_id)
        cyclic = replace(descriptor, snapshot_id=child_id, generation_sequence=2)
        with pytest.raises(LifecycleOperationError) as cycle:
            _ancestor_ids(conn, cyclic, 10)
        assert cycle.value.code == "GENERATION_LINEAGE_CORRUPT"

        conn.execute("DELETE FROM snapshot_generations")
        insert(child_id, descriptor.task_id, 1, baseline_id)
        with pytest.raises(LifecycleOperationError) as missing_parent:
            _ancestor_ids(conn, child, 10)
        assert missing_parent.value.code == "GENERATION_LINEAGE_CORRUPT"

        conn.execute("DELETE FROM snapshot_generations")
        insert(child_id, descriptor.task_id, 0, None)
        insert(sibling_id, other_task_id, 0, None)
        unrelated = replace(
            descriptor,
            snapshot_id=sibling_id,
            task_id=other_task_id,
            generation_sequence=0,
        )
        cross_task_left = replace(descriptor, snapshot_id=child_id, generation_sequence=0)
        assert _lineage_relation(conn, cross_task_left, unrelated, 10) is LineageRelation.UNRELATED


def test_hard_secret_content_is_excluded_and_target_file_is_never_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineage = _build_lineage(tmp_path, (), hard_secret="TOKEN=one\n")
    (lineage.repo / ".env").write_text("TOKEN=two-with-different-bytes\n", encoding="utf-8")
    refreshed = TaskLifecycleService(home=lineage.home).refresh_working(
        ActorContext(ActorKind.CODEX, "test-codex", "refresh-secret"),
        RefreshWorkingRequest(
            idempotency_key="refresh-secret",
            task_context=lineage.contexts[-1],
            write_pause_acknowledged=True,
        ),
    )
    service = LifecycleComparisonService(home=lineage.home)
    left = service.selectors.resolve(_exact_selector(lineage, lineage.baseline_id))
    right = service.selectors.resolve(_exact_selector(lineage, refreshed.snapshot_id))
    original_open = Path.open

    def deny_secret_open(path: Path, *args: object, **kwargs: object):
        if path.name == ".env":
            raise AssertionError("comparison attempted to open target hard-secret content")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_secret_open)
    result = service.compare(
        ComparisonRequest(
            left=left,
            right=right,
            mode=ComparisonMode.ASSERT_EQUIVALENT,
        )
    )
    assert result.equivalent

    for descriptor in (left, right):
        with sqlite3.connect(descriptor.path) as conn:
            row = conn.execute(
                """SELECT p.proof_class, p.content_hash, f.content_hash
                   FROM proof_manifest p JOIN files f ON f.file_id = p.file_id
                   WHERE p.relative_path = '.env'"""
            ).fetchone()
        assert row == ("EXCLUDED_HARD_SECRET", None, None)


def test_disposable_dogfood_exact_reads_and_comparison(tmp_path: Path) -> None:
    root = Path(tempfile.mkdtemp(prefix="stage7d-dogfood-", dir=tmp_path))
    evidence: dict[str, object] = {}
    try:
        lineage = _build_lineage(root, ("alpha",), hard_secret="TOKEN=dogfood\n")
        reads = LifecycleReadService(home=lineage.home)
        comparisons = LifecycleComparisonService(home=lineage.home)
        timings: dict[str, int] = {}

        started = time.perf_counter_ns()
        baseline = reads.resolve(
            _task_selector(
                lineage,
                LifecycleSelectorKind.TASK_BASELINE,
                lineage.baseline_pointer_version,
            )
        )
        timings["baseline_alias_resolve"] = time.perf_counter_ns() - started
        baseline_context, baseline_rows = reads.symbols(baseline, name="baseline")
        assert baseline_rows
        assert baseline_context["snapshot_source"] == "LIFECYCLE_GENERATION"

        pinned_working = reads.resolve(
            _task_selector(
                lineage,
                LifecycleSelectorKind.TASK_LATEST_WORKING,
                lineage.working_pointer_versions[0],
            )
        )
        pinned_context_before, pinned_rows_before = reads.symbols(
            pinned_working,
            name="alpha",
        )
        assert pinned_rows_before
        started = time.perf_counter_ns()
        pinned_currentness = reads.currentness(
            pinned_working,
            mode=VerificationMode.STRONG,
        )
        timings["pinned_strong_currentness"] = time.perf_counter_ns() - started
        assert pinned_currentness.result is not None
        assert pinned_currentness.result.state is CurrentnessState.CURRENT
        mutation_evidence_before = _read_nonmutation_evidence(
            lineage.home,
            lineage.repo,
            lineage.project_id,
        )
        equivalent = TaskLifecycleService(home=lineage.home).refresh_working(
            ActorContext(ActorKind.CODEX, "dogfood-codex", "dogfood-refresh"),
            RefreshWorkingRequest(
                idempotency_key="dogfood-refresh",
                task_context=lineage.contexts[-1],
                write_pause_acknowledged=True,
            ),
        )
        assert equivalent.task_context.working_pointer_version is not None

        started = time.perf_counter_ns()
        latest = reads.resolve(
            _task_selector(
                lineage,
                LifecycleSelectorKind.TASK_LATEST_WORKING,
                equivalent.task_context.working_pointer_version,
            )
        )
        timings["exact_latest_resolve"] = time.perf_counter_ns() - started

        started = time.perf_counter_ns()
        query_context, query_rows = reads.symbols(latest, name="alpha")
        timings["exact_query"] = time.perf_counter_ns() - started
        assert query_rows
        assert query_context["snapshot_source"] == "LIFECYCLE_GENERATION"
        pinned_context_after, pinned_rows_after = reads.symbols(
            pinned_working,
            name="alpha",
        )
        assert pinned_working.snapshot_id == lineage.working_ids[0]
        assert pinned_context_before == pinned_context_after
        assert pinned_rows_before == pinned_rows_after
        pinned_gate = reads.gate(
            pinned_working,
            (GateRequirement.REPO_VALID, GateRequirement.SNAPSHOT_CURRENT),
            currentness=pinned_currentness,
        )
        assert pinned_gate.result.allowed

        counts_before = _lifecycle_read_counts(lineage.home)
        started = time.perf_counter_ns()
        diff = comparisons.compare(
            ComparisonRequest(
                left=_exact_selector(lineage, lineage.baseline_id),
                right=_exact_selector(lineage, lineage.working_ids[0]),
                mode=ComparisonMode.DIFF,
                page_size=2,
            )
        )
        timings["baseline_working_diff"] = time.perf_counter_ns() - started
        filtered_duplicate = comparisons.compare(
            ComparisonRequest(
                left=_exact_selector(lineage, lineage.baseline_id),
                right=_exact_selector(lineage, lineage.working_ids[0]),
                mode=ComparisonMode.DIFF,
                path_prefixes=("module.py", "module.py"),
                page_size=1,
            )
        )
        filtered_normalized = comparisons.compare(
            ComparisonRequest(
                left=_exact_selector(lineage, lineage.baseline_id),
                right=_exact_selector(lineage, lineage.working_ids[0]),
                mode=ComparisonMode.DIFF,
                path_prefixes=("module.py",),
                page_size=1,
            )
        )
        assert filtered_duplicate.comparison_id == filtered_normalized.comparison_id
        assert filtered_duplicate.next_cursor == filtered_normalized.next_cursor
        started = time.perf_counter_ns()
        different_assertion = comparisons.compare(
            ComparisonRequest(
                left=_exact_selector(lineage, lineage.baseline_id),
                right=_exact_selector(lineage, lineage.working_ids[0]),
                mode=ComparisonMode.ASSERT_EQUIVALENT,
            )
        )
        timings["baseline_working_assertion"] = time.perf_counter_ns() - started
        started = time.perf_counter_ns()
        equivalent_assertion = comparisons.compare(
            ComparisonRequest(
                left=_exact_selector(lineage, lineage.working_ids[0]),
                right=_exact_selector(lineage, equivalent.snapshot_id),
                mode=ComparisonMode.ASSERT_EQUIVALENT,
            )
        )
        timings["working_equivalence_assertion"] = time.perf_counter_ns() - started

        module_left = root / "module-key-left.sqlite"
        module_right = root / "module-key-right.sqlite"
        shutil.copy2(pinned_working.path, module_left)
        shutil.copy2(pinned_working.path, module_right)
        for path in (module_left, module_right):
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """UPDATE files SET module_resolution_status = 'EXACT',
                                        module_name = 'pkg.module', is_importable = 1
                       WHERE relative_path = 'module.py'"""
                )
        with sqlite3.connect(module_right) as conn:
            conn.execute(
                """UPDATE files SET path_key = 'moved/module.py',
                                    relative_path = 'moved/module.py'
                   WHERE relative_path = 'module.py'"""
            )
        module_key_changes = _changes("modules", module_left, module_right)
        assert len(module_key_changes) == 1
        assert module_key_changes[0].kind is ChangeKind.MODIFIED

        symbol_left = root / "symbol-key-left.sqlite"
        symbol_right = root / "symbol-key-right.sqlite"
        shutil.copy2(pinned_working.path, symbol_left)
        shutil.copy2(pinned_working.path, symbol_right)
        with sqlite3.connect(symbol_right) as conn:
            conn.execute("UPDATE symbols SET symbol_kind = 'CLASS' WHERE symbol_kind = 'FUNCTION'")
        symbol_key_changes = [
            change
            for change in _changes("symbols", symbol_left, symbol_right)
            if change.logical_key["symbol_kind"] in {"FUNCTION", "CLASS"}
        ]
        assert {change.kind for change in symbol_key_changes} == {
            ChangeKind.REMOVED,
            ChangeKind.ADDED,
        }

        with sqlite3.connect(pinned_working.path) as conn:
            hard_secret = conn.execute(
                """SELECT p.proof_class, p.content_hash, f.content_hash
                   FROM proof_manifest p JOIN files f ON f.file_id = p.file_id
                   WHERE p.relative_path = '.env'"""
            ).fetchone()
        assert hard_secret == ("EXCLUDED_HARD_SECRET", None, None)

        assert not diff.equivalent
        assert diff.next_cursor is not None
        assert different_assertion.outcome == "DIFFERENT"
        assert equivalent_assertion.outcome == "EQUIVALENT"
        _close_task(lineage)
        with closing(sqlite3.connect(registry_path(lineage.home))) as conn:
            conn.execute(
                """UPDATE workspaces
                   SET workspace_state = 'RETIRED', row_version = row_version + 1
                   WHERE workspace_id = ?""",
                (lineage.workspace_id,),
            )
            conn.commit()
        stale_gate = reads.gate(
            pinned_working,
            (GateRequirement.REPO_VALID, GateRequirement.SNAPSHOT_CURRENT),
            currentness=pinned_currentness,
        )
        assert not stale_gate.result.allowed
        assert stale_gate.binding_reason_code == "WORKSPACE_NOT_ACTIVE"
        historical = reads.resolve(_exact_selector(lineage, pinned_working.snapshot_id))
        historical_context, historical_rows = reads.symbols(historical, name="alpha")
        historical_currentness = reads.currentness(historical, mode=VerificationMode.STRONG)
        assert historical_context["snapshot_source"] == "HISTORICAL_GENERATION"
        assert historical_rows
        assert not historical_currentness.applicable
        assert _lifecycle_read_counts(lineage.home) == counts_before
        mutation_evidence_after = _read_nonmutation_evidence(
            lineage.home,
            lineage.repo,
            lineage.project_id,
        )
        assert mutation_evidence_after == mutation_evidence_before
        evidence = {
            "task_id": lineage.task_id,
            "baseline_snapshot_id": lineage.baseline_id,
            "working_snapshot_id": lineage.working_ids[0],
            "equivalent_snapshot_id": equivalent.snapshot_id,
            "baseline_pointer_version": lineage.baseline_pointer_version,
            "working_pointer_version": lineage.working_pointer_versions[0],
            "latest_pointer_version": equivalent.task_context.working_pointer_version,
            "diff_comparison_id": diff.comparison_id,
            "diff_changed_groups": diff.total_changed_groups,
            "different_assertion_id": different_assertion.comparison_id,
            "different_assertion_outcome": different_assertion.outcome,
            "equivalence_comparison_id": equivalent_assertion.comparison_id,
            "equivalence_outcome": equivalent_assertion.outcome,
            "filtered_comparison_id": filtered_duplicate.comparison_id,
            "filtered_cursor": filtered_duplicate.next_cursor,
            "diff_lineage": diff.lineage.value if diff.lineage else None,
            "equivalence_lineage": (
                equivalent_assertion.lineage.value if equivalent_assertion.lineage else None
            ),
            "currentness": pinned_currentness.state,
            "gate_allowed_after_pointer_move": pinned_gate.result.allowed,
            "stale_gate_allowed_after_retirement": stale_gate.result.allowed,
            "stale_gate_reason_code": stale_gate.binding_reason_code,
            "historical_source": historical.snapshot_source.value,
            "historical_currentness": historical_currentness.state,
            "module_key_change_kinds": [change.kind.value for change in module_key_changes],
            "symbol_key_change_kinds": [change.kind.value for change in symbol_key_changes],
            "hard_secret_no_hash": hard_secret[1:] == (None, None),
            "timings_ns": timings,
            "pinned_descriptor_unchanged_after_pointer_move": True,
            "target_head_branch_index_config_git_manifest_unchanged": True,
            "target_worktree_manifest_unchanged": True,
            "registry_read_counts_unchanged": True,
            "canonical_and_legacy_run_evidence_unchanged": True,
        }
        print(f"STAGE7D_DOGFOOD={json.dumps(evidence, sort_keys=True)}")
    finally:
        gc.collect()
        shutil.rmtree(root, onexc=_remove_readonly)
    assert evidence
    assert not root.exists()


def _changes(dimension: str, left: Path, right: Path):
    return list(
        _dimension_changes(
            _SPEC_BY_NAME[dimension],
            left,
            right,
            budget=_ScanBudget(100_000),
            max_group_rows=10_000,
        )
    )


def _rewrite_cursor(cursor: str, *, version: int) -> str:
    padding = "=" * (-len(cursor) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(f"{cursor}{padding}").decode("utf-8"))
    body = {
        "version": version,
        "comparison_id": envelope["comparison_id"],
        "offset": envelope["offset"],
    }
    body["checksum"] = hashlib.sha256(_bytes(body)).hexdigest()
    return base64.urlsafe_b64encode(_bytes(body)).decode("ascii").rstrip("=")


def _read_nonmutation_evidence(
    home: Path,
    repo: Path,
    project_id: str,
) -> dict[str, object]:
    with closing(sqlite3.connect(registry_path(home))) as conn:
        storage_path = Path(
            conn.execute(
                "SELECT storage_path FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
        )
    git_directory = Path(_git(repo, "rev-parse", "--git-dir"))
    if not git_directory.is_absolute():
        git_directory = repo / git_directory
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "symbolic-ref", "--quiet", "HEAD"),
        "status": _git(repo, "status", "--porcelain=v2"),
        "git_index": _file_hash(git_directory / "index"),
        "git_config": _file_hash(git_directory / "config"),
        "git_manifest": _tree_manifest(git_directory),
        "worktree_manifest": _tree_manifest(repo, excluded_root=git_directory),
        "legacy_storage_manifest": _tree_manifest(storage_path),
        "canonical_hash": _file_hash(storage_path / "kb.sqlite"),
    }


def _tree_manifest(
    root: Path,
    *,
    excluded_root: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    if not root.exists():
        return ()
    excluded = excluded_root.resolve() if excluded_root is not None else None
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if excluded is not None and (resolved == excluded or excluded in resolved.parents):
            continue
        rows.append((path.relative_to(root).as_posix(), _file_hash(path) or ""))
    return tuple(rows)


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lifecycle_read_counts(home: Path) -> tuple[int, ...]:
    with closing(sqlite3.connect(registry_path(home))) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "lifecycle_tasks",
                "lifecycle_operations",
                "snapshot_generations",
                "managed_pointers",
            )
        )


def _remove_readonly(function: object, path: str, error: BaseException) -> None:
    del error
    os.chmod(path, stat.S_IWRITE)
    function(path)  # type: ignore[operator]


def _bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
