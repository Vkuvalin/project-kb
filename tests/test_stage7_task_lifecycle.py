"""Stage 7C BEGIN_TASK and REFRESH_WORKING lifecycle contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _git_support import git

from project_kb.errors import LifecycleOperationError
from project_kb.git_utils import DETACHED_HEAD_REF, observe_git_workspace
from project_kb.indexing import capture as capture_module
from project_kb.indexing.models import ScanPolicy
from project_kb.indexing.service import IndexService
from project_kb.lifecycle.generation import (
    GenerationReservationRequest,
    OperationLease,
    OperationReservation,
)
from project_kb.lifecycle.task import (
    ActorContext,
    ActorKind,
    BeginTaskRequest,
    CodexTaskLifecycleFacade,
    PrivilegedTaskLifecycleFacade,
    RecoveryTaskLifecycleFacade,
    RefreshWorkingRequest,
    TaskContext,
    TaskLifecycleService,
    TaskOperationResult,
)
from project_kb.registry.models import ProjectRecord
from project_kb.registry.service import RegistryService
from project_kb.resolver.repo_identity import build_repository_fingerprint, normalize_path

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="the managed lifecycle publisher is Windows-only in Slice 7"
)


class _CrashAfterFilePublication(BaseException):
    pass


def _actor(kind: ActorKind = ActorKind.USER) -> ActorContext:
    return ActorContext(
        kind=kind,
        subject="stage-7c-test",
        invocation_id=f"invocation-{kind.value.lower()}",
    )


def _register(home: Path, repo: Path) -> ProjectRecord:
    project = RegistryService(home=home).register("repo-one", repo).project
    assert project is not None
    return project


def _attach_linked_workspace(
    home: Path,
    project: ProjectRecord,
    primary_repo: Path,
    linked_repo: Path,
) -> ProjectRecord:
    git(primary_repo, "worktree", "add", "-b", "linked-task", str(linked_repo))
    workspace_id = uuid.uuid4().hex
    binding_generation = uuid.uuid4().hex
    workspace_root = str(linked_repo.resolve())
    workspace_root_norm = normalize_path(linked_repo)
    fingerprint_json = build_repository_fingerprint(linked_repo).to_json()
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """INSERT INTO workspaces (
                   workspace_id, project_id, workspace_root, workspace_root_norm,
                   workspace_kind, repository_fingerprint_json,
                   workspace_binding_generation, workspace_state, row_version,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'LINKED', ?, ?, 'ACTIVE', 0, ?, ?)""",
            (
                workspace_id,
                project.project_id,
                workspace_root,
                workspace_root_norm,
                fingerprint_json,
                binding_generation,
                timestamp,
                timestamp,
            ),
        )
    return replace(
        project,
        workspace_id=workspace_id,
        workspace_kind="LINKED",
        workspace_state="ACTIVE",
        workspace_row_version=0,
        repo_root=workspace_root,
        repo_root_norm=workspace_root_norm,
        repo_fingerprint_json=fingerprint_json,
        repo_binding_generation=binding_generation,
    )


def _project_projection(home: Path, project_id: str) -> dict[str, object]:
    row = _row(
        home,
        """SELECT repo_root, repo_root_norm, repo_fingerprint_json,
                  repo_binding_generation, last_status, last_indexed_at,
                  last_git_commit, snapshot_binding_generation
           FROM projects WHERE project_id = ?""",
        (project_id,),
    )
    return dict(row)


def _recovery_service(
    home: Path,
    now: list[datetime],
    *,
    crash_after_file: bool,
) -> TaskLifecycleService:
    def clock() -> datetime:
        return now[0]

    def lease(actor: ActorContext, key: str) -> OperationLease:
        return OperationLease(
            owner=f"{actor.kind.value}:{actor.subject}",
            token=f"lease-{key}-{now[0].timestamp()}",
            duration_seconds=1,
        )

    def checkpoint(name: str) -> None:
        if crash_after_file and name == "after_file_publication":
            raise _CrashAfterFilePublication()

    return TaskLifecycleService(
        home=home,
        clock=clock,
        lease_factory=lease,
        checkpoint=checkpoint,
    )


def _begin_request(
    project: ProjectRecord,
    repo: Path,
    *,
    key: str = "begin-task",
    expected_head: str | None = None,
    expected_ref: str | None = None,
) -> BeginTaskRequest:
    observation = observe_git_workspace(repo)
    assert observation.head is not None
    return BeginTaskRequest(
        idempotency_key=key,
        project_id=project.project_id,
        workspace_id=project.workspace_id,
        workspace_binding_generation=project.repo_binding_generation,
        expected_git_head=expected_head or observation.head,
        expected_head_ref=expected_ref or observation.head_ref,
    )


def _begin(
    home: Path,
    repo: Path,
    *,
    service: TaskLifecycleService | None = None,
    key: str = "begin-task",
) -> tuple[ProjectRecord, TaskLifecycleService, BeginTaskRequest, TaskOperationResult]:
    project = _register(home, repo)
    resolved_service = service or TaskLifecycleService(home=home)
    request = _begin_request(project, repo, key=key)
    result = PrivilegedTaskLifecycleFacade(resolved_service, _actor()).begin_task(request)
    return project, resolved_service, request, result


def _refresh(
    service: TaskLifecycleService,
    context: TaskContext,
    *,
    key: str,
) -> TaskOperationResult:
    return CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX)).refresh_working(
        RefreshWorkingRequest(
            idempotency_key=key,
            task_context=context,
            write_pause_acknowledged=True,
        )
    )


def _row(home: Path, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row:
    with sqlite3.connect(home / "registry.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, parameters).fetchone()
    assert row is not None
    return row


def _count(home: Path, table: str) -> int:
    with sqlite3.connect(home / "registry.sqlite") as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _json_failure(row: sqlite3.Row) -> dict[str, object]:
    value = json.loads(row["failure_json"])
    assert isinstance(value, dict)
    return value


def _git_status(repo: Path) -> bytes:
    result = git(repo, "status", "--porcelain=v2", "--branch", "--untracked-files=all")
    assert isinstance(result.stdout, bytes)
    return result.stdout


def _path_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _run_file_bytes(storage_path: Path) -> dict[str, bytes]:
    runs = storage_path / "runs"
    return {path.name: path.read_bytes() for path in runs.iterdir() if path.is_file()}


def test_begin_task_clean_success_is_atomic_active_sequence_zero_and_replayable(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project, service, request, result = _begin(isolated_kb_home, temp_git_repo)

    assert result.outcome == "SUCCESS"
    assert result.generation_sequence == 0
    assert result.task_context.task_state == "ACTIVE"
    assert result.task_context.latest_working_snapshot_id is None
    assert result.writes_may_resume is True
    assert result.recovery_required is False
    assert result.file_published is True
    assert result.generation_registered is True
    assert result.pointer_updated is True
    assert set(result.timings_ns) >= {
        "preflight",
        "reserve",
        "begin_build_and_recheck",
        "capture",
        "publish_register_cas",
        "total",
    }
    generation = _row(
        isolated_kb_home,
        "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
        (result.snapshot_id,),
    )
    assert generation["generation_sequence"] == 0
    assert generation["parent_snapshot_id"] is None
    task = _row(
        isolated_kb_home,
        "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
        (result.task_context.task_id,),
    )
    assert task["task_state"] == "ACTIVE"
    assert task["capture_contract_fingerprint"] == (
        result.task_context.capture_contract_fingerprint
    )

    replay = PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(request)
    assert replay.outcome == "REPLAYED_SUCCESS"
    assert replay.replayed is True
    assert replay.task_context == result.task_context
    assert replay.operation_id == result.operation_id
    assert replay.snapshot_id == result.snapshot_id
    assert _count(isolated_kb_home, "lifecycle_tasks") == 1
    assert _count(isolated_kb_home, "snapshot_generations") == 1

    status = service.task_status(
        project_id=project.project_id,
        workspace_id=project.workspace_id,
        task_id=result.task_context.task_id,
    )
    assert status.context == result.task_context
    assert status.live_workspace_currentness == "NOT_EVALUATED"
    assert status.last_operation_id == result.operation_id
    assert status.last_operation_phase == "COMMITTED"


@pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
def test_begin_task_rejects_every_dirty_workspace_population(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    dirty_kind: str,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    if dirty_kind == "staged":
        (temp_git_repo / "staged.py").write_text("VALUE = 1\n", encoding="utf-8")
        git(temp_git_repo, "add", "staged.py")
    elif dirty_kind == "unstaged":
        (temp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    else:
        (temp_git_repo / "untracked.py").write_text("VALUE = 1\n", encoding="utf-8")

    facade = PrivilegedTaskLifecycleFacade(TaskLifecycleService(home=isolated_kb_home), _actor())
    with pytest.raises(LifecycleOperationError) as captured:
        facade.begin_task(_begin_request(project, temp_git_repo))

    assert captured.value.code == "WORKSPACE_NOT_CLEAN"
    assert captured.value.details["writes_may_resume"] is True
    assert captured.value.details["recovery_required"] is False
    assert _count(isolated_kb_home, "lifecycle_tasks") == 0


@pytest.mark.parametrize(
    ("changed_field", "expected_code"),
    [("head", "GIT_HEAD_CHANGED"), ("ref", "GIT_HEAD_REF_CHANGED")],
)
def test_begin_task_rejects_wrong_git_expectations(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    changed_field: str,
    expected_code: str,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    request = _begin_request(
        project,
        temp_git_repo,
        expected_head="0" * 40 if changed_field == "head" else None,
        expected_ref="refs/heads/other-branch" if changed_field == "ref" else None,
    )

    facade = PrivilegedTaskLifecycleFacade(TaskLifecycleService(home=isolated_kb_home), _actor())
    with pytest.raises(LifecycleOperationError) as captured:
        facade.begin_task(request)

    assert captured.value.code == expected_code
    assert _count(isolated_kb_home, "lifecycle_tasks") == 0


def test_begin_task_rejects_noncanonical_head_ref_request(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    request = _begin_request(project, temp_git_repo, expected_ref="main")

    with pytest.raises(LifecycleOperationError) as captured:
        PrivilegedTaskLifecycleFacade(
            TaskLifecycleService(home=isolated_kb_home), _actor()
        ).begin_task(request)

    assert captured.value.code == "LIFECYCLE_REQUEST_INVALID"
    assert captured.value.details["field"] == "expected_head_ref"
    assert _count(isolated_kb_home, "lifecycle_tasks") == 0


def test_branch_named_detached_is_distinct_from_detached_head_and_switch_is_detected(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    git(temp_git_repo, "branch", "-m", "DETACHED")
    attached = observe_git_workspace(temp_git_repo)
    assert attached.head_ref == "refs/heads/DETACHED"
    assert attached.head_ref != DETACHED_HEAD_REF

    project = _register(isolated_kb_home, temp_git_repo)
    service = TaskLifecycleService(home=isolated_kb_home)
    begin = PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(
        _begin_request(project, temp_git_repo)
    )
    assert begin.task_context.baseline_head_ref == "refs/heads/DETACHED"

    git(temp_git_repo, "checkout", "--detach")
    detached = observe_git_workspace(temp_git_repo)
    assert detached.head == attached.head
    assert detached.head_ref == DETACHED_HEAD_REF
    with pytest.raises(LifecycleOperationError) as captured:
        _refresh(service, begin.task_context, key="attached-to-detached")
    assert captured.value.code == "GIT_HEAD_REF_CHANGED"
    assert captured.value.details == {
        "expected": "refs/heads/DETACHED",
        "actual": DETACHED_HEAD_REF,
        "writes_may_resume": True,
        "recovery_required": False,
        "file_published": False,
        "generation_registered": False,
        "pointer_updated": False,
    }


def test_begin_task_supports_explicit_detached_head_and_rejects_git_operation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    git(temp_git_repo, "checkout", "--detach")
    detached = _begin_request(project, temp_git_repo)
    assert detached.expected_head_ref == DETACHED_HEAD_REF
    result = PrivilegedTaskLifecycleFacade(
        TaskLifecycleService(home=isolated_kb_home), _actor()
    ).begin_task(detached)
    assert result.task_context.baseline_head_ref == DETACHED_HEAD_REF

    other_home = isolated_kb_home.parent / "other-home"
    other_repo = isolated_kb_home.parent / "other-repo"
    other_repo.mkdir()
    git(other_repo, "init", "-b", "main")
    (other_repo / "README.md").write_text("initial\n", encoding="utf-8")
    git(other_repo, "add", "README.md")
    git(other_repo, "commit", "-m", "initial")
    other_project = _register(other_home, other_repo)
    head = observe_git_workspace(other_repo).head
    assert head is not None
    (other_repo / ".git" / "MERGE_HEAD").write_text(f"{head}\n", encoding="ascii")

    with pytest.raises(LifecycleOperationError) as captured:
        PrivilegedTaskLifecycleFacade(TaskLifecycleService(home=other_home), _actor()).begin_task(
            _begin_request(other_project, other_repo)
        )
    assert captured.value.code == "GIT_OPERATION_IN_PROGRESS"


def test_begin_task_rejects_unmerged_conflict_even_without_operation_marker(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    git(temp_git_repo, "checkout", "-b", "conflicting")
    (temp_git_repo / "README.md").write_text("branch side\n", encoding="utf-8")
    git(temp_git_repo, "add", "README.md")
    git(temp_git_repo, "commit", "-m", "branch side")
    git(temp_git_repo, "checkout", "main")
    (temp_git_repo / "README.md").write_text("main side\n", encoding="utf-8")
    git(temp_git_repo, "add", "README.md")
    git(temp_git_repo, "commit", "-m", "main side")
    with pytest.raises(subprocess.CalledProcessError):
        git(temp_git_repo, "merge", "conflicting")
    (temp_git_repo / ".git" / "MERGE_HEAD").unlink()

    facade = PrivilegedTaskLifecycleFacade(TaskLifecycleService(home=isolated_kb_home), _actor())
    with pytest.raises(LifecycleOperationError) as captured:
        facade.begin_task(_begin_request(project, temp_git_repo))
    assert captured.value.code == "GIT_CONFLICTS_PRESENT"


def test_begin_task_enforces_open_lineage_and_actor_owned_facades(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project, service, _, _ = _begin(isolated_kb_home, temp_git_repo)
    with pytest.raises(LifecycleOperationError) as captured:
        PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(
            _begin_request(project, temp_git_repo, key="second-begin")
        )
    assert captured.value.code == "OPEN_TASK_EXISTS"

    codex = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX))
    assert not hasattr(codex, "begin_task")
    assert not hasattr(codex, "dispatch")
    with pytest.raises(LifecycleOperationError) as actor_error:
        service.begin_task(
            _actor(ActorKind.CODEX),
            _begin_request(project, temp_git_repo, key="codex-begin"),
        )
    assert actor_error.value.code == "ACTOR_NOT_AUTHORIZED"


def test_primary_and_linked_workspaces_own_independent_active_tasks(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    primary = _register(isolated_kb_home, temp_git_repo)
    linked_repo = isolated_kb_home.parent / "linked-worktree"
    linked = _attach_linked_workspace(
        isolated_kb_home,
        primary,
        temp_git_repo,
        linked_repo,
    )
    projection_before = _project_projection(isolated_kb_home, primary.project_id)
    common_storage_before = _path_manifest(temp_git_repo / ".git")
    service = TaskLifecycleService(home=isolated_kb_home)
    privileged = PrivilegedTaskLifecycleFacade(service, _actor())

    primary_begin = privileged.begin_task(
        _begin_request(primary, temp_git_repo, key="primary-begin")
    )
    linked_begin = privileged.begin_task(_begin_request(linked, linked_repo, key="linked-begin"))
    (linked_repo / "linked-change.py").write_text("LINKED = True\n", encoding="utf-8")
    linked_refresh = _refresh(service, linked_begin.task_context, key="linked-refresh")

    assert primary.workspace_id != linked.workspace_id
    assert primary_begin.task_context.workspace_id == primary.workspace_id
    assert linked_begin.task_context.workspace_id == linked.workspace_id
    assert linked_refresh.task_context.workspace_id == linked.workspace_id
    assert linked_refresh.generation_sequence == 1
    assert (
        service.task_status(
            project_id=primary.project_id,
            workspace_id=primary.workspace_id,
            task_id=primary_begin.task_context.task_id,
        ).task_state
        == "ACTIVE"
    )
    assert (
        service.task_status(
            project_id=linked.project_id,
            workspace_id=linked.workspace_id,
            task_id=linked_begin.task_context.task_id,
        ).task_state
        == "ACTIVE"
    )
    assert _count(isolated_kb_home, "lifecycle_tasks") == 2
    assert _count(isolated_kb_home, "snapshot_generations") == 3
    assert _project_projection(isolated_kb_home, primary.project_id) == projection_before
    assert _path_manifest(temp_git_repo / ".git") == common_storage_before


def test_refresh_accepts_dirty_capture_and_chains_lineage(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    _, service, _, begin = _begin(isolated_kb_home, temp_git_repo)
    (temp_git_repo / "README.md").write_text("unstaged refresh\n", encoding="utf-8")
    (temp_git_repo / "staged.py").write_text("STAGED = 1\n", encoding="utf-8")
    (temp_git_repo / "untracked.py").write_text("UNTRACKED = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "staged.py")

    first = _refresh(service, begin.task_context, key="refresh-one")
    assert first.outcome == "SUCCESS"
    assert first.generation_sequence == 1
    assert first.task_context.working_pointer_version == 0
    first_generation = _row(
        isolated_kb_home,
        "SELECT parent_snapshot_id FROM snapshot_generations WHERE snapshot_id = ?",
        (first.snapshot_id,),
    )
    assert first_generation["parent_snapshot_id"] == begin.snapshot_id

    (temp_git_repo / "untracked.py").write_text("UNTRACKED = 2\n", encoding="utf-8")
    second = _refresh(service, first.task_context, key="refresh-two")
    assert second.generation_sequence == 2
    second_generation = _row(
        isolated_kb_home,
        "SELECT parent_snapshot_id FROM snapshot_generations WHERE snapshot_id = ?",
        (second.snapshot_id,),
    )
    assert second_generation["parent_snapshot_id"] == first.snapshot_id
    assert second.task_context.latest_working_snapshot_id == second.snapshot_id
    assert second.task_context.working_pointer_version == 1


def test_refresh_requires_pause_and_exact_fresh_context(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    _, service, _, begin = _begin(isolated_kb_home, temp_git_repo)
    facade = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX))
    with pytest.raises(LifecycleOperationError) as pause_error:
        facade.refresh_working(
            RefreshWorkingRequest(
                idempotency_key="pause-missing",
                task_context=begin.task_context,
                write_pause_acknowledged=False,
            )
        )
    assert pause_error.value.code == "WRITE_PAUSE_REQUIRED"
    assert pause_error.value.details == {
        "write_pause_acknowledged": False,
        "writes_may_resume": True,
        "recovery_required": False,
        "file_published": False,
        "generation_registered": False,
        "pointer_updated": False,
    }

    stale_task = replace(
        begin.task_context, task_row_version=begin.task_context.task_row_version + 1
    )
    with pytest.raises(LifecycleOperationError) as stale_error:
        _refresh(service, stale_task, key="stale-task")
    assert stale_error.value.code == "STALE_TASK_CONTEXT"

    inconsistent_state = replace(begin.task_context, task_state="ABANDONED")
    with pytest.raises(LifecycleOperationError) as state_error:
        _refresh(service, inconsistent_state, key="inconsistent-task-state")
    assert state_error.value.code == "TASK_CONTEXT_MISMATCH"

    incomplete_pointer = replace(begin.task_context, working_pointer_version=0)
    with pytest.raises(LifecycleOperationError) as incomplete_error:
        _refresh(service, incomplete_pointer, key="incomplete-pointer")
    assert incomplete_error.value.code == "TASK_CONTEXT_MISMATCH"

    current = _refresh(service, begin.task_context, key="first-working")
    stale_pointer = replace(
        current.task_context,
        working_pointer_version=current.task_context.working_pointer_version + 1,
    )
    with pytest.raises(LifecycleOperationError) as pointer_error:
        _refresh(service, stale_pointer, key="stale-pointer")
    assert pointer_error.value.code == "STALE_WORKING_POINTER"


def test_refresh_rejects_head_ref_binding_and_contract_drift(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service, _, begin = _begin(isolated_kb_home, temp_git_repo)
    git(temp_git_repo, "checkout", "-b", "changed-ref")
    with pytest.raises(LifecycleOperationError) as ref_error:
        _refresh(service, begin.task_context, key="changed-ref")
    assert ref_error.value.code == "GIT_HEAD_REF_CHANGED"
    git(temp_git_repo, "checkout", "main")

    (temp_git_repo / "README.md").write_text("new commit\n", encoding="utf-8")
    git(temp_git_repo, "add", "README.md")
    git(temp_git_repo, "commit", "-m", "new head")
    with pytest.raises(LifecycleOperationError) as head_error:
        _refresh(service, begin.task_context, key="changed-head")
    assert head_error.value.code == "GIT_HEAD_CHANGED"

    git(temp_git_repo, "reset", "--soft", begin.task_context.baseline_git_head)
    git(temp_git_repo, "reset")
    monkeypatch.setattr(
        "project_kb.lifecycle.task.workspace_matches_repository",
        lambda **_kwargs: False,
    )
    with pytest.raises(LifecycleOperationError) as binding_error:
        _refresh(service, begin.task_context, key="changed-binding")
    assert binding_error.value.code == "WORKSPACE_BINDING_CHANGED"
    monkeypatch.undo()

    incompatible = TaskLifecycleService(
        home=isolated_kb_home,
        policy=ScanPolicy(policy_version="stage-7c-incompatible"),
    )
    with pytest.raises(LifecycleOperationError) as contract_error:
        _refresh(incompatible, begin.task_context, key="changed-contract")
    assert contract_error.value.code == "CAPTURE_CONTRACT_CHANGED"


def test_refresh_committed_replay_is_exact_and_does_not_republish(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    _, service, _, begin = _begin(isolated_kb_home, temp_git_repo)
    request = RefreshWorkingRequest(
        idempotency_key="refresh-replay",
        task_context=begin.task_context,
        write_pause_acknowledged=True,
    )
    facade = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX))
    first = facade.refresh_working(request)
    replay = facade.refresh_working(request)

    assert replay.outcome == "REPLAYED_SUCCESS"
    assert replay.replayed is True
    assert replay.task_context == first.task_context
    assert replay.operation_id == first.operation_id
    assert replay.snapshot_id == first.snapshot_id
    assert _count(isolated_kb_home, "snapshot_generations") == 2


def test_two_concurrent_refreshes_have_one_winner_and_no_lost_update(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service, _, begin = _begin(isolated_kb_home, temp_git_repo)
    barrier = threading.Barrier(2)
    original_reserve = service.generations.reserve_operation

    def synchronized_reserve(
        request: GenerationReservationRequest,
        lease: OperationLease,
    ) -> OperationReservation:
        if request.operation_kind == "REFRESH_WORKING":
            barrier.wait(timeout=10)
        return original_reserve(request, lease)

    monkeypatch.setattr(service.generations, "reserve_operation", synchronized_reserve)
    facade = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX))

    def run(key: str) -> TaskOperationResult:
        return facade.refresh_working(
            RefreshWorkingRequest(
                idempotency_key=key,
                task_context=begin.task_context,
                write_pause_acknowledged=True,
            )
        )

    outcomes: list[TaskOperationResult] = []
    errors: list[LifecycleOperationError] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, key) for key in ("concurrent-a", "concurrent-b")]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=30))
            except LifecycleOperationError as exc:
                errors.append(exc)

    assert len(outcomes) == 1
    assert len(errors) == 1
    assert errors[0].code == "TASK_VERSION_CONFLICT"
    winner = outcomes[0]
    assert winner.generation_sequence == 1
    assert _count(isolated_kb_home, "snapshot_generations") == 2
    assert _count(isolated_kb_home, "managed_pointers") == 2
    status = service.task_status(
        project_id=begin.task_context.project_id,
        workspace_id=begin.task_context.workspace_id,
        task_id=begin.task_context.task_id,
    )
    assert status.context == winner.task_context


def test_begin_prepublication_failure_abandons_draft_and_never_publishes(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)

    def fail_capture(*_args: object, **_kwargs: object) -> object:
        raise LifecycleOperationError("forced prepublication failure", code="FORCED_FAILURE")

    monkeypatch.setattr(capture_module, "capture_trusted_artifact", fail_capture)
    facade = PrivilegedTaskLifecycleFacade(TaskLifecycleService(home=isolated_kb_home), _actor())
    with pytest.raises(LifecycleOperationError) as captured:
        facade.begin_task(_begin_request(project, temp_git_repo))

    assert captured.value.code == "FORCED_FAILURE"
    assert captured.value.details["writes_may_resume"] is True
    assert captured.value.details["file_published"] is False
    task = _row(isolated_kb_home, "SELECT * FROM lifecycle_tasks")
    operation = _row(isolated_kb_home, "SELECT * FROM lifecycle_operations")
    assert task["task_state"] == "ABANDONED"
    assert operation["operation_phase"] == "FAILED"
    assert _count(isolated_kb_home, "snapshot_generations") == 0
    assert _count(isolated_kb_home, "managed_pointers") == 0


def test_postfile_begin_crash_recovers_same_task_without_duplicate_lineage(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return now[0]

    def lease(actor: ActorContext, key: str) -> OperationLease:
        return OperationLease(
            owner=f"{actor.kind.value}:{actor.subject}",
            token=f"lease-{key}-{now[0].timestamp()}",
            duration_seconds=1,
        )

    def checkpoint(name: str) -> None:
        if name == "after_file_publication":
            raise _CrashAfterFilePublication()

    request = _begin_request(project, temp_git_repo, key="postfile-recovery")
    crashing = TaskLifecycleService(
        home=isolated_kb_home,
        clock=clock,
        lease_factory=lease,
        checkpoint=checkpoint,
    )
    with pytest.raises(_CrashAfterFilePublication):
        PrivilegedTaskLifecycleFacade(crashing, _actor()).begin_task(request)
    assert _count(isolated_kb_home, "lifecycle_tasks") == 1
    draft = _row(isolated_kb_home, "SELECT * FROM lifecycle_tasks")
    assert draft["task_state"] == "DRAFT"
    status = crashing.task_status(
        project_id=project.project_id,
        workspace_id=project.workspace_id,
        task_id=draft["task_id"],
    )
    assert status.open_operation_id is not None
    assert status.open_operation_phase == "BUILDING"
    assert status.live_workspace_currentness == "NOT_EVALUATED"

    now[0] += timedelta(seconds=2)
    recovering = TaskLifecycleService(
        home=isolated_kb_home,
        clock=clock,
        lease_factory=lease,
    )
    result = RecoveryTaskLifecycleFacade(
        recovering, _actor(ActorKind.RECOVERY_SERVICE)
    ).recover_begin_task(request)
    assert result.outcome == "SUCCESS"
    assert result.task_context.task_state == "ACTIVE"
    assert _count(isolated_kb_home, "lifecycle_tasks") == 1
    assert _count(isolated_kb_home, "lifecycle_operations") == 1
    assert _count(isolated_kb_home, "snapshot_generations") == 1


def test_postfile_begin_recovery_blocks_dirty_workspace_until_exact_restoration(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    now = [datetime(2026, 7, 18, 13, 0, tzinfo=UTC)]
    request = _begin_request(project, temp_git_repo, key="postfile-dirty-recovery")
    with pytest.raises(_CrashAfterFilePublication):
        PrivilegedTaskLifecycleFacade(
            _recovery_service(isolated_kb_home, now, crash_after_file=True),
            _actor(),
        ).begin_task(request)

    def unexpected_capture(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("published task recovery must not rerun trusted capture")

    monkeypatch.setattr(capture_module, "capture_trusted_artifact", unexpected_capture)
    dirty_path = temp_git_repo / "dirty-after-crash.py"
    dirty_path.write_text("DIRTY = True\n", encoding="utf-8")
    now[0] += timedelta(seconds=2)
    recovery = RecoveryTaskLifecycleFacade(
        _recovery_service(isolated_kb_home, now, crash_after_file=False),
        _actor(ActorKind.RECOVERY_SERVICE),
    )
    with pytest.raises(LifecycleOperationError) as captured:
        recovery.recover_begin_task(request)

    assert captured.value.code == "WORKSPACE_NOT_CLEAN"
    assert captured.value.details["writes_may_resume"] is False
    assert captured.value.details["recovery_required"] is True
    assert captured.value.details["file_published"] is True
    operation = _row(isolated_kb_home, "SELECT * FROM lifecycle_operations")
    assert operation["operation_phase"] == "RECOVERY_REQUIRED"
    assert _json_failure(operation)["reason"] == "TASK_RECOVERY_PRECONDITION_MISMATCH"
    assert _row(isolated_kb_home, "SELECT * FROM lifecycle_tasks")["task_state"] == "DRAFT"
    assert _count(isolated_kb_home, "snapshot_generations") == 0
    assert _count(isolated_kb_home, "managed_pointers") == 0

    dirty_path.unlink()
    now[0] += timedelta(seconds=2)
    recovered = recovery.recover_begin_task(request)
    assert recovered.outcome == "SUCCESS"
    assert recovered.task_context.task_state == "ACTIVE"
    assert _count(isolated_kb_home, "snapshot_generations") == 1
    assert _count(isolated_kb_home, "managed_pointers") == 1


@pytest.mark.parametrize(
    ("drift_kind", "expected_code"),
    [("head", "GIT_HEAD_CHANGED"), ("ref", "GIT_HEAD_REF_CHANGED")],
)
def test_postfile_begin_recovery_blocks_head_or_ref_drift_until_restored(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
    expected_code: str,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    now = [datetime(2026, 7, 18, 14, 0, tzinfo=UTC)]
    request = _begin_request(project, temp_git_repo, key="postfile-head-recovery")
    with pytest.raises(_CrashAfterFilePublication):
        PrivilegedTaskLifecycleFacade(
            _recovery_service(isolated_kb_home, now, crash_after_file=True),
            _actor(),
        ).begin_task(request)

    git(temp_git_repo, "checkout", "-b", "recovery-drift")
    if drift_kind == "head":
        (temp_git_repo / "head-drift.py").write_text("DRIFT = True\n", encoding="utf-8")
        git(temp_git_repo, "add", "head-drift.py")
        git(temp_git_repo, "commit", "-m", "head and ref drift")
    now[0] += timedelta(seconds=2)
    recovery = RecoveryTaskLifecycleFacade(
        _recovery_service(isolated_kb_home, now, crash_after_file=False),
        _actor(ActorKind.RECOVERY_SERVICE),
    )
    with pytest.raises(LifecycleOperationError) as captured:
        recovery.recover_begin_task(request)
    assert captured.value.code == expected_code
    assert captured.value.details["writes_may_resume"] is False
    assert captured.value.details["recovery_required"] is True
    assert (
        _row(isolated_kb_home, "SELECT * FROM lifecycle_operations")["operation_phase"]
        == "RECOVERY_REQUIRED"
    )
    assert _count(isolated_kb_home, "snapshot_generations") == 0
    assert _count(isolated_kb_home, "managed_pointers") == 0

    git(temp_git_repo, "checkout", "main")

    def unexpected_capture(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("published task recovery must not rerun trusted capture")

    monkeypatch.setattr(capture_module, "capture_trusted_artifact", unexpected_capture)
    now[0] += timedelta(seconds=2)
    recovered = recovery.recover_begin_task(request)
    assert recovered.outcome == "SUCCESS"
    assert recovered.task_context.baseline_git_head == request.expected_git_head
    assert recovered.task_context.baseline_head_ref == request.expected_head_ref


def test_postfile_refresh_recovery_blocks_head_ref_drift_without_pointer_movement(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    now = [datetime(2026, 7, 18, 15, 0, tzinfo=UTC)]
    service = _recovery_service(isolated_kb_home, now, crash_after_file=False)
    begin = PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(
        _begin_request(project, temp_git_repo, key="refresh-recovery-begin")
    )
    request = RefreshWorkingRequest(
        idempotency_key="postfile-refresh-recovery",
        task_context=begin.task_context,
        write_pause_acknowledged=True,
    )
    with pytest.raises(_CrashAfterFilePublication):
        CodexTaskLifecycleFacade(
            _recovery_service(isolated_kb_home, now, crash_after_file=True),
            _actor(ActorKind.CODEX),
        ).refresh_working(request)

    git(temp_git_repo, "checkout", "-b", "refresh-recovery-drift")
    (temp_git_repo / "refresh-head-drift.py").write_text("DRIFT = True\n", encoding="utf-8")
    git(temp_git_repo, "add", "refresh-head-drift.py")
    git(temp_git_repo, "commit", "-m", "refresh head and ref drift")
    now[0] += timedelta(seconds=2)
    recovery = RecoveryTaskLifecycleFacade(
        _recovery_service(isolated_kb_home, now, crash_after_file=False),
        _actor(ActorKind.RECOVERY_SERVICE),
    )
    with pytest.raises(LifecycleOperationError) as captured:
        recovery.recover_refresh_working(request)
    assert captured.value.code == "GIT_HEAD_CHANGED"
    assert captured.value.details["writes_may_resume"] is False
    assert captured.value.details["recovery_required"] is True
    refresh_operation = _row(
        isolated_kb_home,
        "SELECT * FROM lifecycle_operations WHERE operation_kind = 'REFRESH_WORKING'",
    )
    assert refresh_operation["operation_phase"] == "RECOVERY_REQUIRED"
    assert _count(isolated_kb_home, "snapshot_generations") == 1
    assert _count(isolated_kb_home, "managed_pointers") == 1

    git(temp_git_repo, "checkout", "main")

    def unexpected_capture(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("published task recovery must not rerun trusted capture")

    monkeypatch.setattr(capture_module, "capture_trusted_artifact", unexpected_capture)
    now[0] += timedelta(seconds=2)
    recovered = recovery.recover_refresh_working(request)
    assert recovered.outcome == "SUCCESS"
    assert recovered.generation_sequence == 1
    assert recovered.task_context.latest_working_snapshot_id == recovered.snapshot_id
    assert _count(isolated_kb_home, "snapshot_generations") == 2
    assert _count(isolated_kb_home, "managed_pointers") == 2


def test_committed_begin_and_refresh_replay_ignore_later_live_git_changes(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    service = TaskLifecycleService(home=isolated_kb_home)
    begin_request = _begin_request(project, temp_git_repo, key="terminal-begin-replay")
    begin = PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(begin_request)
    (temp_git_repo / "later-change.py").write_text("LATER = True\n", encoding="utf-8")
    refresh_request = RefreshWorkingRequest(
        idempotency_key="terminal-refresh-replay",
        task_context=begin.task_context,
        write_pause_acknowledged=True,
    )
    refresh = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX)).refresh_working(
        refresh_request
    )
    git(temp_git_repo, "checkout", "-b", "after-committed-lifecycle")
    git(temp_git_repo, "add", "later-change.py")
    git(temp_git_repo, "commit", "-m", "later legitimate workspace change")

    monkeypatch.setattr(
        "project_kb.lifecycle.task.observe_git_workspace",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("terminal replay must not observe current live Git state")
        ),
    )
    begin_replay = PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(begin_request)
    refresh_replay = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX)).refresh_working(
        refresh_request
    )

    assert begin_replay.outcome == "REPLAYED_SUCCESS"
    assert begin_replay.operation_id == begin.operation_id
    assert begin_replay.snapshot_id == begin.snapshot_id
    assert refresh_replay.outcome == "REPLAYED_SUCCESS"
    assert refresh_replay.operation_id == refresh.operation_id
    assert refresh_replay.snapshot_id == refresh.snapshot_id
    assert refresh_replay.task_context == refresh.task_context
    assert _count(isolated_kb_home, "lifecycle_tasks") == 1
    assert _count(isolated_kb_home, "lifecycle_operations") == 2
    assert _count(isolated_kb_home, "snapshot_generations") == 2


def test_source_mutation_during_refresh_fails_without_advancing_pointer_or_reusing_sequence(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service, _, begin = _begin(isolated_kb_home, temp_git_repo)
    original_scan = capture_module.scan_repository

    def mutating_scan(*args: object, **kwargs: object) -> object:
        result = original_scan(*args, **kwargs)
        (temp_git_repo / "mutated-during-capture.py").write_text(
            "MUTATED = True\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(capture_module, "scan_repository", mutating_scan)
    with pytest.raises(LifecycleOperationError) as captured:
        _refresh(service, begin.task_context, key="mutating-refresh")
    assert captured.value.details["writes_may_resume"] is True
    assert captured.value.details["pointer_updated"] is False
    assert _count(isolated_kb_home, "managed_pointers") == 1
    failed = _row(
        isolated_kb_home,
        "SELECT * FROM lifecycle_operations WHERE operation_kind = 'REFRESH_WORKING'",
    )
    assert failed["operation_phase"] == "FAILED"
    assert failed["reserved_generation_sequence"] == 1

    monkeypatch.undo()
    success = _refresh(
        service,
        service.task_status(
            project_id=begin.task_context.project_id,
            workspace_id=begin.task_context.workspace_id,
            task_id=begin.task_context.task_id,
        ).context,
        key="after-failed-refresh",
    )
    assert success.generation_sequence == 2


def test_lifecycle_does_not_mutate_git_or_legacy_canonical_snapshot(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = _register(isolated_kb_home, temp_git_repo)
    IndexService(home=isolated_kb_home).index("repo-one")
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_before = canonical.read_bytes()
    project_before = RegistryService(home=isolated_kb_home).find_project_by_name("repo-one")
    git_before = _git_status(temp_git_repo)
    git_storage_before = _path_manifest(temp_git_repo / ".git")
    legacy_runs_before = _run_file_bytes(Path(project.storage_path))

    service = TaskLifecycleService(home=isolated_kb_home)
    begin = PrivilegedTaskLifecycleFacade(service, _actor()).begin_task(
        _begin_request(project, temp_git_repo, key="isolation-begin")
    )
    refresh = _refresh(service, begin.task_context, key="isolation-refresh")

    assert begin.generation_sequence == 0
    assert refresh.generation_sequence == 1
    assert canonical.read_bytes() == canonical_before
    assert RegistryService(home=isolated_kb_home).find_project_by_name("repo-one") == project_before
    assert _git_status(temp_git_repo) == git_before
    assert _path_manifest(temp_git_repo / ".git") == git_storage_before
    assert _run_file_bytes(Path(project.storage_path)) == legacy_runs_before
    codex = CodexTaskLifecycleFacade(service, _actor(ActorKind.CODEX))
    for later_slice_surface in ("compare", "accept_task", "reject_task", "cleanup"):
        assert not hasattr(codex, later_slice_surface)
    assert not hasattr(service, "current_task")
