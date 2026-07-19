from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from _git_support import git

from project_kb.errors import LifecycleOperationError, SnapshotQueryError
from project_kb.gating import GateRequirement
from project_kb.git_utils import observe_git_workspace
from project_kb.indexing.query import QueryService
from project_kb.indexing.service import IndexService
from project_kb.lifecycle.read import LifecycleReadService
from project_kb.lifecycle.selector import (
    LifecycleSelectorKind,
    LifecycleSelectorRequest,
    LifecycleSelectorService,
    SnapshotSource,
)
from project_kb.lifecycle.task import (
    ActorContext,
    ActorKind,
    BeginTaskRequest,
    RefreshWorkingRequest,
    TaskContext,
    TaskLifecycleService,
)
from project_kb.registry import RegistryService
from project_kb.registry.db import registry_path
from project_kb.snapshot.currentness import CurrentnessState, VerificationMode


@dataclass(frozen=True)
class _Lineage:
    home: Path
    repo: Path
    project_id: str
    workspace_id: str
    binding_generation: str
    task_id: str
    baseline_id: str
    baseline_pointer_version: int
    working_ids: tuple[str, ...]
    working_pointer_versions: tuple[int, ...]
    contexts: tuple[TaskContext, ...]


def test_exact_selectors_stay_pinned_and_reads_are_source_explicit(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    selectors = LifecycleSelectorService(home=lineage.home)
    reads = LifecycleReadService(home=lineage.home)

    baseline = selectors.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_BASELINE,
            lineage.baseline_pointer_version,
        )
    )
    first_working = selectors.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_LATEST_WORKING,
            lineage.working_pointer_versions[0],
        )
    )
    (lineage.repo / "module.py").write_text(
        "import os\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    refreshed = TaskLifecycleService(home=lineage.home).refresh_working(
        ActorContext(ActorKind.CODEX, "test-codex", "refresh-after-resolve"),
        RefreshWorkingRequest(
            idempotency_key="refresh-after-resolve",
            task_context=lineage.contexts[-1],
            write_pause_acknowledged=True,
        ),
    )
    assert refreshed.task_context.working_pointer_version is not None
    latest = selectors.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_LATEST_WORKING,
            refreshed.task_context.working_pointer_version,
        )
    )

    assert baseline.snapshot_id == lineage.baseline_id
    assert latest.snapshot_id == refreshed.snapshot_id
    assert baseline.snapshot_source is SnapshotSource.LIFECYCLE_GENERATION
    assert first_working.pointer_id is not None
    assert first_working.resolved_pointer_version == lineage.working_pointer_versions[0]
    assert latest.resolved_pointer_version == refreshed.task_context.working_pointer_version
    assert latest.resolved_pointer_predecessor_snapshot_id == lineage.working_ids[0]

    old_context, old_rows = reads.symbols(first_working, name="alpha")
    new_context, new_rows = reads.symbols(latest, name="beta")
    assert old_context["snapshot_source"] == "LIFECYCLE_GENERATION"
    assert old_context["snapshot"]["snapshot_id"] == lineage.working_ids[0]
    assert new_context["snapshot"]["snapshot_id"] == refreshed.snapshot_id
    assert any(row["short_name"] == "alpha" for row in old_rows)
    assert any(row["short_name"] == "beta" for row in new_rows)

    without_currentness = reads.gate(latest, (GateRequirement.SNAPSHOT_CURRENT,))
    assert not without_currentness.result.allowed
    fast = reads.currentness(latest, mode=VerificationMode.FAST)
    assert fast.result is not None
    assert fast.result.state is CurrentnessState.UNVERIFIED
    assert fast.result.reason == "fast_verification_removed"
    assert not (lineage.home / "scratch" / "currentness").exists()
    with pytest.raises(LifecycleOperationError) as invalid_currentness:
        reads.currentness(latest, mode=VerificationMode.STRONG, max_attempts=0)
    assert invalid_currentness.value.code == "CURRENTNESS_REQUEST_INVALID"
    strong = reads.currentness(latest, mode=VerificationMode.STRONG)
    assert strong.result is not None
    assert strong.result.state is CurrentnessState.CURRENT
    exact_latest = selectors.resolve(_exact_selector(lineage, latest.snapshot_id))
    with pytest.raises(LifecycleOperationError) as stale_identity:
        reads.gate(
            latest,
            (GateRequirement.SNAPSHOT_CURRENT,),
            currentness=replace(strong, descriptor=exact_latest),
        )
    assert stale_identity.value.code == "CURRENTNESS_EVIDENCE_MISMATCH"
    present = reads.gate(latest, (GateRequirement.SNAPSHOT_PRESENT,))
    assert present.result.allowed
    with pytest.raises(LifecycleOperationError) as invalid_gate:
        reads.gate(latest, (GateRequirement.USER_APPROVAL,), user_approval="yes")  # type: ignore[arg-type]
    assert invalid_gate.value.code == "GATE_CONTEXT_INVALID"

    with pytest.raises(LifecycleOperationError) as mismatch:
        selectors.resolve(
            _task_selector(
                lineage,
                LifecycleSelectorKind.TASK_LATEST_WORKING,
                lineage.working_pointer_versions[0],
            )
        )
    assert mismatch.value.code == "POINTER_VERSION_MISMATCH"


def test_historical_exact_reads_work_but_aliases_and_currentness_fail_closed(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    selectors = LifecycleSelectorService(home=lineage.home)
    reads = LifecycleReadService(home=lineage.home)
    active = selectors.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_LATEST_WORKING,
            lineage.working_pointer_versions[-1],
        )
    )
    with sqlite3.connect(registry_path(lineage.home)) as conn:
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
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'RETIRED', row_version = row_version + 1
               WHERE workspace_id = ?""",
            (lineage.workspace_id,),
        )

    with pytest.raises(SnapshotQueryError) as stale_active:
        reads.symbols(active, name="alpha")
    assert stale_active.value.details["reason"] == "workspace_not_active"

    historical = selectors.resolve(_exact_selector(lineage, lineage.working_ids[0]))
    assert historical.snapshot_source is SnapshotSource.HISTORICAL_GENERATION
    assert historical.historical_binding_mismatch

    context, rows = reads.symbols(historical, name="alpha")
    assert context["snapshot_source"] == "HISTORICAL_GENERATION"
    assert rows
    currentness = reads.currentness(historical, mode=VerificationMode.STRONG)
    assert not currentness.applicable
    assert currentness.state == "NOT_APPLICABLE"

    with pytest.raises(LifecycleOperationError) as broken:
        selectors.resolve(
            _task_selector(
                lineage,
                LifecycleSelectorKind.TASK_LATEST_WORKING,
                lineage.working_pointer_versions[-1],
            )
        )
    assert broken.value.code == "BROKEN_POINTER"


def test_selector_error_taxonomy_and_blocked_generation_state(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ("alpha", "beta"))
    selectors = LifecycleSelectorService(home=lineage.home)
    descriptor = selectors.resolve(_exact_selector(lineage, lineage.baseline_id))

    for state, expected_code in (
        ("ORPHANED", "GENERATION_NOT_AVAILABLE"),
        ("PENDING_DELETE", "GENERATION_NOT_AVAILABLE"),
        ("DELETED", "GENERATION_DELETED"),
    ):
        with pytest.raises(LifecycleOperationError) as blocked:
            selectors.resolve_reference(replace(descriptor, generation_state=state))
        assert blocked.value.code == expected_code
        assert blocked.value.details["generation_state"] == state

    with pytest.raises(LifecycleOperationError) as forged:
        selectors.resolve_reference(replace(descriptor, capture_contract_fingerprint="0" * 64))
    assert forged.value.code == "DESCRIPTOR_PROVENANCE_INVALID"

    with pytest.raises(LifecycleOperationError) as unsupported:
        selectors.resolve(
            LifecycleSelectorRequest(
                version=99,
                kind=LifecycleSelectorKind.SNAPSHOT_ID,
                project_id=lineage.project_id,
                snapshot_id=lineage.baseline_id,
            )
        )
    assert unsupported.value.code == "SELECTOR_VERSION_UNSUPPORTED"

    with pytest.raises(LifecycleOperationError) as missing:
        selectors.resolve(
            LifecycleSelectorRequest(
                version=1,
                kind=LifecycleSelectorKind.SNAPSHOT_ID,
                project_id=lineage.project_id,
                snapshot_id="f" * 32,
            )
        )
    assert missing.value.code == "SNAPSHOT_NOT_FOUND"

    with pytest.raises(LifecycleOperationError) as final_missing:
        selectors.resolve(_task_selector(lineage, LifecycleSelectorKind.TASK_FINAL, 0))
    assert final_missing.value.code == "TASK_FINAL_NOT_AVAILABLE"

    with pytest.raises(LifecycleOperationError) as project_baseline_missing:
        selectors.resolve(
            LifecycleSelectorRequest(
                version=1,
                kind=LifecycleSelectorKind.PROJECT_BASELINE,
                project_id=lineage.project_id,
                expected_pointer_version=0,
            )
        )
    assert project_baseline_missing.value.code == "PROJECT_BASELINE_NOT_AVAILABLE"

    with sqlite3.connect(registry_path(lineage.home)) as conn:
        conn.execute(
            """UPDATE snapshot_generations
               SET generation_state = 'QUARANTINED', row_version = row_version + 1
               WHERE snapshot_id = ?""",
            (lineage.working_ids[0],),
        )
    with pytest.raises(LifecycleOperationError) as quarantined:
        selectors.resolve(_exact_selector(lineage, lineage.working_ids[0]))
    assert quarantined.value.code == "GENERATION_QUARANTINED"


def test_exact_descriptor_rejects_forged_selector_pointer_and_source_provenance(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    selectors = LifecycleSelectorService(home=lineage.home)
    exact = selectors.resolve(_exact_selector(lineage, lineage.working_ids[0]))
    alias = selectors.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_LATEST_WORKING,
            lineage.working_pointer_versions[0],
        )
    )

    forged_descriptors = (
        replace(exact, pointer_id="f" * 32),
        replace(exact, snapshot_source=SnapshotSource.HISTORICAL_GENERATION),
        replace(alias, pointer_id="f" * 32),
        replace(alias, pointer_kind="TASK_BASELINE"),
        replace(alias, resolved_pointer_version=alias.resolved_pointer_version + 1),
        replace(alias, resolved_pointer_predecessor_snapshot_id="e" * 32),
        replace(alias, historical_binding_mismatch=True),
    )
    for forged in forged_descriptors:
        with pytest.raises(LifecycleOperationError) as rejected:
            selectors.resolve_reference(forged)
        assert rejected.value.code == "DESCRIPTOR_PROVENANCE_INVALID"


def test_stale_currentness_cannot_gate_after_retirement_and_historical_read_survives(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    reads = LifecycleReadService(home=lineage.home)
    descriptor = reads.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_LATEST_WORKING,
            lineage.working_pointer_versions[0],
        )
    )
    currentness = reads.currentness(descriptor, mode=VerificationMode.STRONG)
    assert currentness.result is not None
    assert currentness.result.state is CurrentnessState.CURRENT
    _close_task(lineage)
    with sqlite3.connect(registry_path(lineage.home)) as conn:
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'RETIRED', row_version = row_version + 1
               WHERE workspace_id = ?""",
            (lineage.workspace_id,),
        )

    for requirements in (
        (GateRequirement.REPO_VALID,),
        (GateRequirement.SNAPSHOT_CURRENT,),
        (GateRequirement.REPO_VALID, GateRequirement.SNAPSHOT_CURRENT),
    ):
        gate = reads.gate(descriptor, requirements, currentness=currentness)
        assert not gate.result.allowed
        assert gate.binding_reason_code == "WORKSPACE_NOT_ACTIVE"

    historical = reads.resolve(_exact_selector(lineage, lineage.working_ids[0]))
    context, rows = reads.symbols(historical, name="alpha")
    assert context["snapshot_source"] == "HISTORICAL_GENERATION"
    assert rows
    historical_currentness = reads.currentness(historical, mode=VerificationMode.STRONG)
    assert not historical_currentness.applicable


def test_stale_currentness_cannot_gate_after_binding_root_or_identity_drift(
    tmp_path: Path,
) -> None:
    lineage = _build_lineage(tmp_path, ("alpha",))
    reads = LifecycleReadService(home=lineage.home)
    descriptor = reads.resolve(
        _task_selector(
            lineage,
            LifecycleSelectorKind.TASK_LATEST_WORKING,
            lineage.working_pointer_versions[0],
        )
    )
    currentness = reads.currentness(descriptor, mode=VerificationMode.STRONG)
    assert currentness.result is not None
    assert currentness.result.state is CurrentnessState.CURRENT
    _close_task(lineage)
    with sqlite3.connect(registry_path(lineage.home)) as conn:
        original = conn.execute(
            """SELECT workspace_root, workspace_root_norm,
                      repository_fingerprint_json, workspace_binding_generation
               FROM workspaces WHERE workspace_id = ?""",
            (lineage.workspace_id,),
        ).fetchone()
    assert original is not None

    mutations = (
        (
            "workspace_binding_generation = ?",
            ("f" * 32,),
            "WORKSPACE_BINDING_GENERATION_CHANGED",
        ),
        (
            "workspace_root = ?, workspace_root_norm = ?",
            (str(tmp_path / "replacement"), str(tmp_path / "replacement")),
            "WORKSPACE_ROOT_CHANGED",
        ),
        (
            "repository_fingerprint_json = ?",
            ('{"drift":true}',),
            "REPOSITORY_IDENTITY_CHANGED",
        ),
    )
    for assignment, values, expected_reason in mutations:
        with sqlite3.connect(registry_path(lineage.home)) as conn:
            conn.execute(
                f"""UPDATE workspaces SET {assignment}, row_version = row_version + 1
                    WHERE workspace_id = ?""",
                (*values, lineage.workspace_id),
            )
        for requirements in (
            (GateRequirement.REPO_VALID,),
            (GateRequirement.SNAPSHOT_CURRENT,),
            (GateRequirement.REPO_VALID, GateRequirement.SNAPSHOT_CURRENT),
        ):
            gate = reads.gate(descriptor, requirements, currentness=currentness)
            assert not gate.result.allowed
            assert gate.binding_reason_code == expected_reason
        with sqlite3.connect(registry_path(lineage.home)) as conn:
            conn.execute(
                """UPDATE workspaces
                   SET workspace_root = ?, workspace_root_norm = ?,
                       repository_fingerprint_json = ?,
                       workspace_binding_generation = ?, row_version = row_version + 1
                   WHERE workspace_id = ?""",
                (*original, lineage.workspace_id),
            )


def test_missing_and_tampered_generation_files_have_precise_errors(tmp_path: Path) -> None:
    lineage = _build_lineage(tmp_path, ())
    selectors = LifecycleSelectorService(home=lineage.home)
    request = _exact_selector(lineage, lineage.baseline_id)
    descriptor = selectors.resolve(request)
    original = descriptor.path.read_bytes()
    descriptor.path.unlink()
    with pytest.raises(LifecycleOperationError) as missing:
        selectors.resolve(request)
    assert missing.value.code == "GENERATION_FILE_MISSING"

    descriptor.path.write_bytes(original + b"tamper")
    with pytest.raises(LifecycleOperationError) as tampered:
        selectors.resolve(request)
    assert tampered.value.code in {
        "GENERATION_INTEGRITY_FAILED",
        "GENERATION_IDENTITY_MISMATCH",
    }


def test_legacy_status_and_query_paths_report_canonical_provenance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    _init_repo(repo, "canonical")
    project = RegistryService(home=home).register("repo-one", repo).project
    assert project is not None
    _commit_registration_marker(repo)
    outcome = IndexService(home=home).index("repo-one")
    assert outcome.result in {"success", "success_with_warnings"}

    status = QueryService(home=home, working_directory=repo).status_service.status("repo-one")
    assert status.data()["snapshot_source"] == "LEGACY_CANONICAL"
    context, rows = QueryService(home=home, working_directory=repo).symbols(
        "repo-one",
        file=None,
        name="canonical",
    )
    assert context["snapshot_source"] == "LEGACY_CANONICAL"
    assert context["snapshot"]["snapshot_source"] == "LEGACY_CANONICAL"
    assert rows


def _close_task(lineage: _Lineage) -> None:
    with sqlite3.connect(registry_path(lineage.home)) as conn:
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


def _build_lineage(
    tmp_path: Path,
    working_functions: tuple[str, ...],
    *,
    hard_secret: str | None = None,
) -> _Lineage:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    _init_repo(repo, "baseline")
    if hard_secret is not None:
        (repo / ".env").write_text(hard_secret, encoding="utf-8")
        _git(repo, "add", ".env")
        _git(repo, "commit", "-m", "add synthetic hard secret")
    project = RegistryService(home=home).register("repo-one", repo).project
    assert project is not None
    _commit_registration_marker(repo)
    head = _git(repo, "rev-parse", "HEAD")
    branch = f"refs/heads/{_git(repo, 'branch', '--show-current')}"
    observation = observe_git_workspace(repo)
    assert not observation.staged_paths, observation
    assert not observation.unstaged_tracked_paths, observation
    assert not observation.untracked_paths, observation
    service = TaskLifecycleService(home=home)
    begin = service.begin_task(
        ActorContext(ActorKind.USER, "test-user", "begin-invocation"),
        BeginTaskRequest(
            idempotency_key="begin-task",
            project_id=project.project_id,
            workspace_id=project.workspace_id,
            workspace_binding_generation=project.repo_binding_generation,
            expected_git_head=head,
            expected_head_ref=branch,
        ),
    )
    contexts = [begin.task_context]
    working_ids: list[str] = []
    working_versions: list[int] = []
    for index, function_name in enumerate(working_functions, start=1):
        (repo / "module.py").write_text(
            f"import os\n\ndef {function_name}():\n    return {index}\n",
            encoding="utf-8",
        )
        refreshed = service.refresh_working(
            ActorContext(ActorKind.CODEX, "test-codex", f"refresh-{index}"),
            RefreshWorkingRequest(
                idempotency_key=f"refresh-{index}",
                task_context=contexts[-1],
                write_pause_acknowledged=True,
            ),
        )
        contexts.append(refreshed.task_context)
        working_ids.append(refreshed.snapshot_id)
        assert refreshed.task_context.working_pointer_version is not None
        working_versions.append(refreshed.task_context.working_pointer_version)
    return _Lineage(
        home=home,
        repo=repo,
        project_id=project.project_id,
        workspace_id=project.workspace_id,
        binding_generation=project.repo_binding_generation,
        task_id=begin.task_context.task_id,
        baseline_id=begin.snapshot_id,
        baseline_pointer_version=begin.task_context.baseline_pointer_version,
        working_ids=tuple(working_ids),
        working_pointer_versions=tuple(working_versions),
        contexts=tuple(contexts),
    )


def _task_selector(
    lineage: _Lineage,
    kind: LifecycleSelectorKind,
    pointer_version: int,
) -> LifecycleSelectorRequest:
    return LifecycleSelectorRequest(
        version=1,
        kind=kind,
        project_id=lineage.project_id,
        task_id=lineage.task_id,
        expected_pointer_version=pointer_version,
    )


def _exact_selector(lineage: _Lineage, snapshot_id: str) -> LifecycleSelectorRequest:
    return LifecycleSelectorRequest(
        version=1,
        kind=LifecycleSelectorKind.SNAPSHOT_ID,
        project_id=lineage.project_id,
        snapshot_id=snapshot_id,
    )


def _init_repo(repo: Path, function_name: str) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    (repo / "module.py").write_text(
        f"import os\n\ndef {function_name}():\n    return 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "initial")


def _commit_registration_marker(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", "registration checkpoint")


def _git(repo: Path, *args: str) -> str:
    result = git(repo, *args, text=True)
    return result.stdout.strip()
