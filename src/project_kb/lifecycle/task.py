"""Explicit active-task lifecycle orchestration for Stage 7 Slice 7C."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from project_kb.errors import LifecycleOperationError, ProjectKbError
from project_kb.git_utils import (
    DETACHED_HEAD_REF,
    GitWorkspaceObservation,
    full_head_ref_observation,
    observe_git_workspace,
)
from project_kb.indexing import capture as capture_module
from project_kb.indexing.capture import (
    CaptureAttempt,
    CaptureContract,
    CaptureRequest,
    CaptureWorkspace,
)
from project_kb.indexing.models import ScanPolicy
from project_kb.lifecycle.generation import (
    BeginTaskReservation,
    GenerationPublicationResult,
    GenerationReservationRequest,
    LifecycleGenerationService,
    OperationLease,
    OperationReservation,
    PointerExpectation,
    capture_contract_fingerprint,
)
from project_kb.registry.db import immediate_registry_transaction, open_existing_registry
from project_kb.registry.service import RegistryService
from project_kb.resolver.repo_check import workspace_matches_repository
from project_kb.storage.home import resolve_home

_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_HEAD_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_UNRESOLVED_PHASES = frozenset({"RESERVED", "BUILDING", "FILE_PUBLISHED", "RECOVERY_REQUIRED"})
_TERMINAL_PHASES = frozenset({"COMMITTED", "FAILED"})
_TASK_CONTEXT_VERSION = 1
_FULL_HEAD_REF_PREFIX = "refs/heads/"
_FORBIDDEN_HEAD_REF_CHARACTERS = frozenset({" ", "~", "^", ":", "?", "*", "[", "\\"})


class ActorKind(StrEnum):
    USER = "USER"
    COMMAND_CENTER = "COMMAND_CENTER"
    CODEX = "CODEX"
    RECOVERY_SERVICE = "RECOVERY_SERVICE"


@dataclass(frozen=True)
class ActorContext:
    """Invocation-bound actor evidence; request payloads never select this value."""

    kind: ActorKind
    subject: str
    invocation_id: str


@dataclass(frozen=True)
class BeginTaskRequest:
    idempotency_key: str
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    expected_git_head: str
    expected_head_ref: str


@dataclass(frozen=True)
class TaskContext:
    """Versioned optimistic-concurrency evidence for one exact task state."""

    version: int
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    task_id: str
    task_state: str
    task_row_version: int
    baseline_snapshot_id: str
    baseline_pointer_version: int
    latest_working_snapshot_id: str | None
    working_pointer_version: int | None
    baseline_git_head: str
    baseline_head_ref: str
    capture_contract_fingerprint: str
    base_project_baseline_snapshot_id: str | None
    base_project_baseline_pointer_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RefreshWorkingRequest:
    idempotency_key: str
    task_context: TaskContext
    write_pause_acknowledged: bool


@dataclass(frozen=True)
class TaskOperationResult:
    outcome: str
    task_context: TaskContext
    operation_id: str
    snapshot_id: str
    generation_sequence: int
    replayed: bool
    writes_may_resume: bool
    recovery_required: bool
    file_published: bool
    generation_registered: bool
    pointer_updated: bool
    timings_ns: Mapping[str, int]


@dataclass(frozen=True)
class TaskStatus:
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    task_id: str
    task_state: str
    task_row_version: int
    baseline_snapshot_id: str | None
    baseline_pointer_version: int | None
    latest_working_snapshot_id: str | None
    working_pointer_version: int | None
    capture_contract_fingerprint: str
    baseline_git_head: str
    baseline_head_ref: str
    open_operation_id: str | None
    open_operation_phase: str | None
    last_operation_id: str | None
    last_operation_phase: str | None
    blocked_or_terminal_reason: Mapping[str, Any] | None
    live_workspace_currentness: str
    context: TaskContext | None


class PrivilegedTaskLifecycleFacade:
    """Narrow USER/COMMAND_CENTER surface; it exposes no generic dispatcher."""

    def __init__(self, service: TaskLifecycleService, actor: ActorContext) -> None:
        _require_actor(actor, frozenset({ActorKind.USER, ActorKind.COMMAND_CENTER}))
        self._service = service
        self._actor = actor

    def begin_task(self, request: BeginTaskRequest) -> TaskOperationResult:
        return self._service.begin_task(self._actor, request)

    def task_status(
        self,
        *,
        project_id: str,
        workspace_id: str,
        task_id: str,
    ) -> TaskStatus:
        return self._service.task_status(
            project_id=project_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )


class CodexTaskLifecycleFacade:
    """Codex-safe surface for refresh and explicit task status only."""

    def __init__(self, service: TaskLifecycleService, actor: ActorContext) -> None:
        _require_actor(
            actor,
            frozenset({ActorKind.USER, ActorKind.COMMAND_CENTER, ActorKind.CODEX}),
        )
        self._service = service
        self._actor = actor

    def refresh_working(self, request: RefreshWorkingRequest) -> TaskOperationResult:
        return self._service.refresh_working(self._actor, request)

    def task_status(
        self,
        *,
        project_id: str,
        workspace_id: str,
        task_id: str,
    ) -> TaskStatus:
        return self._service.task_status(
            project_id=project_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )


class RecoveryTaskLifecycleFacade:
    """Exact-operation recovery; it cannot authorize a new lifecycle operation."""

    def __init__(self, service: TaskLifecycleService, actor: ActorContext) -> None:
        _require_actor(actor, frozenset({ActorKind.RECOVERY_SERVICE}))
        self._service = service
        self._actor = actor

    def recover_begin_task(self, request: BeginTaskRequest) -> TaskOperationResult:
        return self._service.begin_task(self._actor, request, recovery_only=True)

    def recover_refresh_working(self, request: RefreshWorkingRequest) -> TaskOperationResult:
        return self._service.refresh_working(self._actor, request, recovery_only=True)


class TaskLifecycleService:
    """Owned internal orchestration over the trusted capture and 7B2 publisher."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        policy: ScanPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        lease_factory: Callable[[ActorContext, str], OperationLease] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.home = (home or resolve_home()).resolve()
        self.policy = policy or ScanPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lease_factory = lease_factory or self._default_lease
        self.registry = RegistryService(home=self.home)
        self.generations = LifecycleGenerationService(
            home=self.home,
            clock=self._clock,
            id_factory=self._id_factory,
            checkpoint=checkpoint,
        )

    def begin_task(
        self,
        actor: ActorContext,
        request: BeginTaskRequest,
        *,
        recovery_only: bool = False,
    ) -> TaskOperationResult:
        allowed = (
            frozenset({ActorKind.RECOVERY_SERVICE})
            if recovery_only
            else frozenset({ActorKind.USER, ActorKind.COMMAND_CENTER})
        )
        _require_actor(actor, allowed)
        try:
            _validate_begin_request(request)
        except ProjectKbError as exc:
            raise self._decorate_error(exc, getattr(request, "idempotency_key", "")) from exc
        invocation_fingerprint = _fingerprint(
            {
                "operation_kind": "BEGIN_TASK",
                "project_id": request.project_id,
                "workspace_id": request.workspace_id,
                "workspace_binding_generation": request.workspace_binding_generation,
                "expected_git_head": request.expected_git_head,
                "expected_head_ref": request.expected_head_ref,
            }
        )
        reservation: OperationReservation | None = None
        timings: dict[str, int] = {}
        started = time.perf_counter_ns()
        try:
            durable_request = self.generations.request_for_idempotency_key(request.idempotency_key)
            lease = self._lease_factory(actor, request.idempotency_key)
            if durable_request is not None:
                self._require_matching_operation(
                    durable_request,
                    operation_kind="BEGIN_TASK",
                    invocation_fingerprint=invocation_fingerprint,
                )
                reservation = self.generations.reserve_operation(durable_request, lease)
                publication = self._resume_or_capture(
                    reservation,
                    durable_request,
                    lease,
                    require_clean=True,
                )
                timings["total"] = time.perf_counter_ns() - started
                return self._successful_result(publication, "BEGIN_TASK", timings)
            if recovery_only:
                raise LifecycleOperationError(
                    "No previously authorized BEGIN_TASK operation uses this key.",
                    code="OPERATION_NOT_FOUND",
                    details={"idempotency_key": request.idempotency_key},
                )

            preflight_start = time.perf_counter_ns()
            scope = self._require_scope(
                project_id=request.project_id,
                workspace_id=request.workspace_id,
                workspace_binding_generation=request.workspace_binding_generation,
            )
            observation = observe_git_workspace(Path(scope["workspace_root_norm"]))
            self._require_git_state(
                observation,
                expected_head=request.expected_git_head,
                expected_head_ref=request.expected_head_ref,
                require_clean=True,
            )
            baseline = self._project_baseline(request.project_id)
            self._require_no_open_lineage(
                request.workspace_id,
                request.workspace_binding_generation,
            )
            contract = CaptureContract.current(self.policy)
            contract_fingerprint = capture_contract_fingerprint(contract)
            task_id = self._new_id()
            pointer_id = self._new_id()
            stored_ref = _stored_head_ref(request.expected_head_ref)
            generation_request = GenerationReservationRequest(
                idempotency_key=request.idempotency_key,
                operation_kind="BEGIN_TASK",
                project_id=request.project_id,
                workspace_id=request.workspace_id,
                workspace_binding_generation=request.workspace_binding_generation,
                task_id=task_id,
                expected_task_version=0,
                parent_snapshot_id=None,
                pointer_expectations=(
                    PointerExpectation(
                        pointer_id=pointer_id,
                        pointer_role="TASK_BASELINE",
                        expected_pointer_version=None,
                        expected_snapshot_id=None,
                    ),
                ),
                expected_head=request.expected_git_head,
                expected_branch=stored_ref,
                actor_context=_operation_actor_context(actor, invocation_fingerprint),
                activate_task_on_commit=True,
            )
            begin_reservation = BeginTaskReservation(
                capture_contract_fingerprint=contract_fingerprint,
                baseline_head_commit=request.expected_git_head,
                baseline_head_ref=stored_ref,
                project_baseline_snapshot_id=(None if baseline is None else baseline[0]),
                project_baseline_pointer_version=(None if baseline is None else baseline[1]),
            )
            timings["preflight"] = time.perf_counter_ns() - preflight_start
            reserve_start = time.perf_counter_ns()
            reservation = self.generations.reserve_begin_task_operation(
                generation_request,
                lease,
                begin_reservation,
            )
            timings["reserve"] = time.perf_counter_ns() - reserve_start
            publication = self._capture_and_publish(
                reservation,
                generation_request,
                require_clean=True,
                timings=timings,
            )
            timings["total"] = time.perf_counter_ns() - started
            return self._successful_result(publication, "BEGIN_TASK", timings)
        except ProjectKbError as exc:
            if reservation is not None:
                self._classify_operation_failure(reservation, exc, abandon_draft=True)
            raise self._decorate_error(exc, request.idempotency_key) from exc
        except Exception as exc:
            error = LifecycleOperationError(
                "BEGIN_TASK failed inside its bounded internal orchestration.",
                code="BEGIN_TASK_FAILED",
                details={"cause_type": type(exc).__name__, "message": str(exc)},
            )
            if reservation is not None:
                self._classify_operation_failure(reservation, error, abandon_draft=True)
            raise self._decorate_error(error, request.idempotency_key) from exc

    def refresh_working(
        self,
        actor: ActorContext,
        request: RefreshWorkingRequest,
        *,
        recovery_only: bool = False,
    ) -> TaskOperationResult:
        allowed = (
            frozenset({ActorKind.RECOVERY_SERVICE})
            if recovery_only
            else frozenset({ActorKind.USER, ActorKind.COMMAND_CENTER, ActorKind.CODEX})
        )
        _require_actor(actor, allowed)
        try:
            _validate_refresh_request(request)
        except ProjectKbError as exc:
            raise self._decorate_error(exc, getattr(request, "idempotency_key", "")) from exc
        invocation_fingerprint = _fingerprint(
            {
                "operation_kind": "REFRESH_WORKING",
                "task_context": request.task_context.to_dict(),
                "write_pause_acknowledged": request.write_pause_acknowledged,
            }
        )
        reservation: OperationReservation | None = None
        timings: dict[str, int] = {}
        started = time.perf_counter_ns()
        try:
            durable_request = self.generations.request_for_idempotency_key(request.idempotency_key)
            lease = self._lease_factory(actor, request.idempotency_key)
            if durable_request is not None:
                self._require_matching_operation(
                    durable_request,
                    operation_kind="REFRESH_WORKING",
                    invocation_fingerprint=invocation_fingerprint,
                )
                reservation = self.generations.reserve_operation(durable_request, lease)
                publication = self._resume_or_capture(
                    reservation,
                    durable_request,
                    lease,
                    require_clean=False,
                )
                timings["total"] = time.perf_counter_ns() - started
                return self._successful_result(publication, "REFRESH_WORKING", timings)
            if recovery_only:
                raise LifecycleOperationError(
                    "No previously authorized REFRESH_WORKING operation uses this key.",
                    code="OPERATION_NOT_FOUND",
                    details={"idempotency_key": request.idempotency_key},
                )

            preflight_start = time.perf_counter_ns()
            state = self._require_current_context(request.task_context)
            current_contract = CaptureContract.current(self.policy)
            if (
                capture_contract_fingerprint(current_contract)
                != state["task"]["capture_contract_fingerprint"]
            ):
                raise LifecycleOperationError(
                    "The task-pinned capture contract is not reproducible by this runtime.",
                    code="CAPTURE_CONTRACT_CHANGED",
                    details={"task_id": request.task_context.task_id},
                )
            scope = self._require_scope(
                project_id=request.task_context.project_id,
                workspace_id=request.task_context.workspace_id,
                workspace_binding_generation=request.task_context.workspace_binding_generation,
            )
            observation = observe_git_workspace(Path(scope["workspace_root_norm"]))
            self._require_git_state(
                observation,
                expected_head=request.task_context.baseline_git_head,
                expected_head_ref=request.task_context.baseline_head_ref,
                require_clean=False,
            )
            working = state["working"]
            if working is None:
                pointer_id = self._new_id()
                parent_snapshot_id = state["baseline"]["snapshot_id"]
                expected_pointer_version = None
                expected_pointer_snapshot = None
            else:
                pointer_id = working["pointer_id"]
                parent_snapshot_id = working["snapshot_id"]
                expected_pointer_version = working["pointer_version"]
                expected_pointer_snapshot = working["snapshot_id"]
            generation_request = GenerationReservationRequest(
                idempotency_key=request.idempotency_key,
                operation_kind="REFRESH_WORKING",
                project_id=request.task_context.project_id,
                workspace_id=request.task_context.workspace_id,
                workspace_binding_generation=request.task_context.workspace_binding_generation,
                task_id=request.task_context.task_id,
                expected_task_version=request.task_context.task_row_version,
                parent_snapshot_id=parent_snapshot_id,
                pointer_expectations=(
                    PointerExpectation(
                        pointer_id=pointer_id,
                        pointer_role="TASK_LATEST_WORKING",
                        expected_pointer_version=expected_pointer_version,
                        expected_snapshot_id=expected_pointer_snapshot,
                    ),
                ),
                expected_head=request.task_context.baseline_git_head,
                expected_branch=_stored_head_ref(request.task_context.baseline_head_ref),
                actor_context=_operation_actor_context(actor, invocation_fingerprint),
            )
            timings["preflight"] = time.perf_counter_ns() - preflight_start
            reserve_start = time.perf_counter_ns()
            reservation = self.generations.reserve_operation(generation_request, lease)
            timings["reserve"] = time.perf_counter_ns() - reserve_start
            publication = self._capture_and_publish(
                reservation,
                generation_request,
                require_clean=False,
                timings=timings,
            )
            timings["total"] = time.perf_counter_ns() - started
            return self._successful_result(publication, "REFRESH_WORKING", timings)
        except ProjectKbError as exc:
            if reservation is not None:
                self._classify_operation_failure(reservation, exc, abandon_draft=False)
            raise self._decorate_error(exc, request.idempotency_key) from exc
        except Exception as exc:
            error = LifecycleOperationError(
                "REFRESH_WORKING failed inside its bounded internal orchestration.",
                code="REFRESH_WORKING_FAILED",
                details={"cause_type": type(exc).__name__, "message": str(exc)},
            )
            if reservation is not None:
                self._classify_operation_failure(reservation, error, abandon_draft=False)
            raise self._decorate_error(error, request.idempotency_key) from exc

    def task_status(
        self,
        *,
        project_id: str,
        workspace_id: str,
        task_id: str,
    ) -> TaskStatus:
        for field, value in (
            ("project_id", project_id),
            ("workspace_id", workspace_id),
            ("task_id", task_id),
        ):
            _require_id(value, field)
        with self._registry() as conn:
            task = conn.execute(
                """SELECT * FROM lifecycle_tasks
                   WHERE task_id = ? AND project_id = ? AND workspace_id = ?""",
                (task_id, project_id, workspace_id),
            ).fetchone()
            if task is None:
                raise LifecycleOperationError(
                    "Exact lifecycle task was not found.",
                    code="TASK_NOT_FOUND",
                    details={"task_id": task_id},
                )
            baseline = self._pointer_row(conn, task_id, "TASK_BASELINE")
            working = self._pointer_row(conn, task_id, "TASK_LATEST_WORKING")
            open_operation = conn.execute(
                """SELECT * FROM lifecycle_operations
                   WHERE task_id = ? AND operation_phase IN (
                       'RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED'
                   ) ORDER BY created_at DESC, operation_id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            last_operation = conn.execute(
                """SELECT * FROM lifecycle_operations
                   WHERE task_id = ? ORDER BY created_at DESC, operation_id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            context = self._context_from_rows(task, baseline, working) if baseline else None
            reason = None
            reason_source = open_operation or last_operation
            if reason_source is not None and reason_source["failure_json"]:
                reason = _json_object(reason_source["failure_json"])
            return TaskStatus(
                project_id=task["project_id"],
                workspace_id=task["workspace_id"],
                workspace_binding_generation=task["workspace_binding_generation"],
                task_id=task["task_id"],
                task_state=task["task_state"],
                task_row_version=task["row_version"],
                baseline_snapshot_id=None if baseline is None else baseline["snapshot_id"],
                baseline_pointer_version=(
                    None if baseline is None else baseline["pointer_version"]
                ),
                latest_working_snapshot_id=(None if working is None else working["snapshot_id"]),
                working_pointer_version=(None if working is None else working["pointer_version"]),
                capture_contract_fingerprint=task["capture_contract_fingerprint"],
                baseline_git_head=task["baseline_head_commit"],
                baseline_head_ref=_public_head_ref(task["baseline_head_ref"]),
                open_operation_id=(
                    None if open_operation is None else open_operation["operation_id"]
                ),
                open_operation_phase=(
                    None if open_operation is None else open_operation["operation_phase"]
                ),
                last_operation_id=(
                    None if last_operation is None else last_operation["operation_id"]
                ),
                last_operation_phase=(
                    None if last_operation is None else last_operation["operation_phase"]
                ),
                blocked_or_terminal_reason=reason,
                live_workspace_currentness="NOT_EVALUATED",
                context=context,
            )

    def _resume_or_capture(
        self,
        reservation: OperationReservation,
        request: GenerationReservationRequest,
        lease: OperationLease,
        *,
        require_clean: bool,
    ) -> GenerationPublicationResult:
        if reservation.operation_phase in _TERMINAL_PHASES:
            return self.generations.recover_operation(request, lease)
        recovery_evidence = reservation.operation_phase in {
            "FILE_PUBLISHED",
            "RECOVERY_REQUIRED",
        } or any(os.path.lexists(path) for path in _operation_artifact_paths(reservation))
        if recovery_evidence:
            try:
                self._require_nonterminal_recovery_preconditions(
                    reservation,
                    require_clean=require_clean,
                )
            except ProjectKbError as exc:
                if not self._mark_task_recovery_required(reservation, exc):
                    return self.generations.recover_operation(request, lease)
                raise
            return self.generations.recover_operation(request, lease)
        return self._capture_and_publish(
            reservation,
            request,
            require_clean=require_clean,
            timings={},
        )

    def _capture_and_publish(
        self,
        reservation: OperationReservation,
        request: GenerationReservationRequest,
        *,
        require_clean: bool,
        timings: dict[str, int],
    ) -> GenerationPublicationResult:
        build_start = time.perf_counter_ns()
        reservation = self.generations.begin_build(reservation)
        contract = CaptureContract.current(self.policy)
        if capture_contract_fingerprint(contract) != reservation.capture_contract_fingerprint:
            raise LifecycleOperationError(
                "The installed runtime cannot reproduce the task-pinned capture contract.",
                code="CAPTURE_CONTRACT_CHANGED",
                details={"task_id": reservation.task_id},
            )
        if not self._binding_matches(reservation):
            raise LifecycleOperationError(
                "Workspace binding changed before trusted capture.",
                code="WORKSPACE_BINDING_CHANGED",
                details={"workspace_id": reservation.workspace_id},
            )
        observation = observe_git_workspace(Path(reservation.repository_root_norm))
        self._require_git_state(
            observation,
            expected_head=reservation.expected_head,
            expected_head_ref=_public_head_ref(reservation.expected_branch),
            require_clean=require_clean,
        )
        timings["begin_build_and_recheck"] = time.perf_counter_ns() - build_start
        capture_start = time.perf_counter_ns()
        workspace = CaptureWorkspace(
            project_id=reservation.project_id,
            workspace_id=reservation.workspace_id,
            repo_root_norm=reservation.repository_root_norm,
            repository_identity_hash=reservation.repository_identity_hash,
            binding_generation=reservation.workspace_binding_generation,
        )
        with full_head_ref_observation():
            descriptor = capture_module.capture_trusted_artifact(
                CaptureRequest(
                    workspace=workspace,
                    repository_root=Path(reservation.repository_root_norm),
                    attempts=(
                        CaptureAttempt(
                            artifact_path=reservation.paths.temp_path,
                            snapshot_id=reservation.snapshot_id,
                            run_id=self._new_id(),
                        ),
                    ),
                    contract=contract,
                    workspace_matches_repository=lambda actual_root, expected: (
                        workspace_matches_repository(
                            registry=self.registry,
                            repository_root=actual_root,
                            project_id=expected.project_id,
                            workspace_id=expected.workspace_id,
                            workspace_root_norm=expected.repo_root_norm,
                            expected_repository_identity_hash=(expected.repository_identity_hash),
                            workspace_binding_generation=expected.binding_generation,
                        )
                    ),
                )
            )
        timings["capture"] = time.perf_counter_ns() - capture_start
        publish_start = time.perf_counter_ns()
        result = self.generations.publish_artifact(reservation, descriptor)
        timings["publish_register_cas"] = time.perf_counter_ns() - publish_start
        return result

    def _successful_result(
        self,
        publication: GenerationPublicationResult,
        operation_kind: str,
        timings: Mapping[str, int],
    ) -> TaskOperationResult:
        context = self._context_for_publication(publication, operation_kind)
        return TaskOperationResult(
            outcome="REPLAYED_SUCCESS" if publication.replayed else "SUCCESS",
            task_context=context,
            operation_id=publication.operation_id,
            snapshot_id=publication.snapshot_id,
            generation_sequence=publication.generation_sequence,
            replayed=publication.replayed,
            writes_may_resume=True,
            recovery_required=False,
            file_published=True,
            generation_registered=True,
            pointer_updated=True,
            timings_ns=dict(timings),
        )

    def _context_for_publication(
        self,
        publication: GenerationPublicationResult,
        operation_kind: str,
    ) -> TaskContext:
        with self._registry() as conn:
            operation = conn.execute(
                "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
                (publication.operation_id,),
            ).fetchone()
            if operation is None:
                raise LifecycleOperationError(
                    "Committed operation evidence disappeared.",
                    code="OPERATION_RECOVERY_REQUIRED",
                )
            task = conn.execute(
                "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
                (operation["task_id"],),
            ).fetchone()
            baseline = self._pointer_row(conn, operation["task_id"], "TASK_BASELINE")
            if task is None or baseline is None:
                raise LifecycleOperationError(
                    "Committed task baseline evidence is incomplete.",
                    code="BROKEN_POINTER",
                )
            if operation_kind == "BEGIN_TASK":
                working_snapshot = None
                working_version = None
            else:
                working_snapshot = publication.snapshot_id
                working_version = dict(publication.pointer_versions)["TASK_LATEST_WORKING"]
            return TaskContext(
                version=_TASK_CONTEXT_VERSION,
                project_id=task["project_id"],
                workspace_id=task["workspace_id"],
                workspace_binding_generation=task["workspace_binding_generation"],
                task_id=task["task_id"],
                task_state=publication.task_state,
                task_row_version=publication.task_row_version,
                baseline_snapshot_id=baseline["snapshot_id"],
                baseline_pointer_version=baseline["pointer_version"],
                latest_working_snapshot_id=working_snapshot,
                working_pointer_version=working_version,
                baseline_git_head=task["baseline_head_commit"],
                baseline_head_ref=_public_head_ref(task["baseline_head_ref"]),
                capture_contract_fingerprint=task["capture_contract_fingerprint"],
                base_project_baseline_snapshot_id=task["project_baseline_snapshot_id"],
                base_project_baseline_pointer_version=task["project_baseline_pointer_version"],
            )

    def _require_current_context(self, context: TaskContext) -> dict[str, sqlite3.Row | None]:
        if context.version != _TASK_CONTEXT_VERSION:
            raise LifecycleOperationError(
                "TaskContext version is unsupported.",
                code="TASK_CONTEXT_VERSION_UNSUPPORTED",
            )
        with self._registry() as conn:
            task = conn.execute(
                "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
                (context.task_id,),
            ).fetchone()
            if task is None:
                raise LifecycleOperationError(
                    "TaskContext names a missing task.",
                    code="TASK_NOT_FOUND",
                    details={"task_id": context.task_id},
                )
            ownership = (
                task["project_id"],
                task["workspace_id"],
                task["workspace_binding_generation"],
            )
            expected_ownership = (
                context.project_id,
                context.workspace_id,
                context.workspace_binding_generation,
            )
            if ownership != expected_ownership:
                raise LifecycleOperationError(
                    "TaskContext ownership or workspace binding changed.",
                    code="WORKSPACE_BINDING_CHANGED",
                )
            if task["task_state"] != "ACTIVE":
                raise LifecycleOperationError(
                    "TaskContext does not name an ACTIVE task.",
                    code="TASK_STATE_CONFLICT",
                    details={"task_state": task["task_state"]},
                )
            if context.task_state != task["task_state"]:
                raise LifecycleOperationError(
                    "TaskContext task-state evidence is inconsistent.",
                    code="TASK_CONTEXT_MISMATCH",
                    details={"field": "task_state"},
                )
            if task["row_version"] != context.task_row_version:
                raise LifecycleOperationError(
                    "TaskContext row version is stale.",
                    code="STALE_TASK_CONTEXT",
                    details={
                        "expected": context.task_row_version,
                        "actual": task["row_version"],
                    },
                )
            immutable_context = {
                "baseline_git_head": task["baseline_head_commit"],
                "baseline_head_ref": _public_head_ref(task["baseline_head_ref"]),
                "capture_contract_fingerprint": task["capture_contract_fingerprint"],
                "base_project_baseline_snapshot_id": task["project_baseline_snapshot_id"],
                "base_project_baseline_pointer_version": task["project_baseline_pointer_version"],
            }
            for field, actual in immutable_context.items():
                if getattr(context, field) != actual:
                    raise LifecycleOperationError(
                        "TaskContext immutable evidence is inconsistent.",
                        code="TASK_CONTEXT_MISMATCH",
                        details={"field": field},
                    )
            baseline = self._pointer_row(conn, context.task_id, "TASK_BASELINE")
            working = self._pointer_row(conn, context.task_id, "TASK_LATEST_WORKING")
            if (
                baseline is None
                or baseline["snapshot_id"] != context.baseline_snapshot_id
                or baseline["pointer_version"] != context.baseline_pointer_version
            ):
                raise LifecycleOperationError(
                    "TaskContext baseline pointer evidence is inconsistent.",
                    code="BROKEN_POINTER",
                )
            actual_working = (
                None if working is None else (working["snapshot_id"], working["pointer_version"])
            )
            if (context.latest_working_snapshot_id is None) != (
                context.working_pointer_version is None
            ):
                raise LifecycleOperationError(
                    "TaskContext working pointer fields must be present together.",
                    code="TASK_CONTEXT_MISMATCH",
                    details={"field": "latest_working_pointer"},
                )
            expected_working = (
                None
                if context.latest_working_snapshot_id is None
                else (
                    context.latest_working_snapshot_id,
                    context.working_pointer_version,
                )
            )
            if actual_working != expected_working:
                raise LifecycleOperationError(
                    "TaskContext latest-working pointer evidence is stale.",
                    code="STALE_WORKING_POINTER",
                    details={"expected": expected_working, "actual": actual_working},
                )
            return {"task": task, "baseline": baseline, "working": working}

    def _require_nonterminal_recovery_preconditions(
        self,
        reservation: OperationReservation,
        *,
        require_clean: bool,
    ) -> None:
        """Revalidate the task capture contract before nonterminal finalization."""

        with self._registry() as conn:
            operation = conn.execute(
                "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
                (reservation.operation_id,),
            ).fetchone()
            if operation is None:
                raise LifecycleOperationError(
                    "Durable task operation evidence disappeared before recovery.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={"operation_id": reservation.operation_id},
                )
            if operation["operation_phase"] in _TERMINAL_PHASES:
                return
            if operation["operation_phase"] not in _UNRESOLVED_PHASES:
                raise LifecycleOperationError(
                    "Durable task operation has an unsupported recovery phase.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={
                        "operation_id": reservation.operation_id,
                        "operation_phase": operation["operation_phase"],
                    },
                )
            task = conn.execute(
                "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
                (reservation.task_id,),
            ).fetchone()
            if task is None:
                raise LifecycleOperationError(
                    "Durable task evidence disappeared before recovery.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={
                        "operation_id": reservation.operation_id,
                        "task_id": reservation.task_id,
                    },
                )

            expected_task_state = "DRAFT" if reservation.activate_task_on_commit else "ACTIVE"
            operation_expected = {
                "operation_kind": reservation.operation_kind,
                "project_id": reservation.project_id,
                "workspace_id": reservation.workspace_id,
                "workspace_binding_generation": reservation.workspace_binding_generation,
                "task_id": reservation.task_id,
                "reserved_snapshot_id": reservation.snapshot_id,
                "reserved_generation_sequence": reservation.generation_sequence,
            }
            task_expected = {
                "project_id": reservation.project_id,
                "workspace_id": reservation.workspace_id,
                "workspace_binding_generation": reservation.workspace_binding_generation,
                "task_state": expected_task_state,
                "row_version": reservation.reserved_task_version,
                "next_generation_sequence": reservation.generation_sequence + 1,
                "capture_contract_fingerprint": reservation.capture_contract_fingerprint,
                "baseline_head_commit": reservation.expected_head,
                "baseline_head_ref": reservation.expected_branch,
            }
            mismatches: dict[str, dict[str, object]] = {}
            for field, expected in operation_expected.items():
                actual = operation[field]
                if actual != expected:
                    mismatches[f"operation.{field}"] = {
                        "expected": expected,
                        "actual": actual,
                    }
            for field, expected in task_expected.items():
                actual = task[field]
                if actual != expected:
                    mismatches[f"task.{field}"] = {
                        "expected": expected,
                        "actual": actual,
                    }
            if mismatches:
                raise LifecycleOperationError(
                    "Durable task facts no longer match the pinned recovery contract.",
                    code="TASK_RECOVERY_PRECONDITION_FAILED",
                    details={
                        "operation_id": reservation.operation_id,
                        "task_id": reservation.task_id,
                        "mismatches": mismatches,
                    },
                )

        scope = self._require_scope(
            project_id=reservation.project_id,
            workspace_id=reservation.workspace_id,
            workspace_binding_generation=reservation.workspace_binding_generation,
        )
        observation = observe_git_workspace(Path(scope["workspace_root_norm"]))
        self._require_git_state(
            observation,
            expected_head=reservation.expected_head,
            expected_head_ref=_public_head_ref(reservation.expected_branch),
            require_clean=require_clean,
        )

    def _context_from_rows(
        self,
        task: sqlite3.Row,
        baseline: sqlite3.Row,
        working: sqlite3.Row | None,
    ) -> TaskContext:
        return TaskContext(
            version=_TASK_CONTEXT_VERSION,
            project_id=task["project_id"],
            workspace_id=task["workspace_id"],
            workspace_binding_generation=task["workspace_binding_generation"],
            task_id=task["task_id"],
            task_state=task["task_state"],
            task_row_version=task["row_version"],
            baseline_snapshot_id=baseline["snapshot_id"],
            baseline_pointer_version=baseline["pointer_version"],
            latest_working_snapshot_id=None if working is None else working["snapshot_id"],
            working_pointer_version=None if working is None else working["pointer_version"],
            baseline_git_head=task["baseline_head_commit"],
            baseline_head_ref=_public_head_ref(task["baseline_head_ref"]),
            capture_contract_fingerprint=task["capture_contract_fingerprint"],
            base_project_baseline_snapshot_id=task["project_baseline_snapshot_id"],
            base_project_baseline_pointer_version=task["project_baseline_pointer_version"],
        )

    def _require_scope(
        self,
        *,
        project_id: str,
        workspace_id: str,
        workspace_binding_generation: str,
    ) -> sqlite3.Row:
        with self._registry() as conn:
            row = conn.execute(
                """SELECT w.*, p.repo_root AS projected_root,
                          p.repo_root_norm AS projected_root_norm,
                          p.repo_fingerprint_json AS projected_fingerprint_json,
                          p.repo_binding_generation AS projected_binding_generation
                   FROM workspaces AS w JOIN projects AS p ON p.project_id = w.project_id
                   WHERE w.workspace_id = ? AND w.project_id = ?""",
                (workspace_id, project_id),
            ).fetchone()
            projection_matches = row is not None and (
                row["workspace_kind"] != "PRIMARY"
                or (
                    row["workspace_root"] == row["projected_root"]
                    and row["workspace_root_norm"] == row["projected_root_norm"]
                    and row["repository_fingerprint_json"] == row["projected_fingerprint_json"]
                    and row["workspace_binding_generation"] == row["projected_binding_generation"]
                )
            )
            if row is None or (
                row["workspace_state"] != "ACTIVE"
                or row["workspace_binding_generation"] != workspace_binding_generation
                or row["repository_fingerprint_json"] is None
                or not projection_matches
            ):
                raise LifecycleOperationError(
                    "Project/workspace binding is unavailable, stale, or projected incorrectly.",
                    code="WORKSPACE_BINDING_CHANGED",
                    details={"workspace_id": workspace_id},
                )
        if not workspace_matches_repository(
            registry=self.registry,
            repository_root=Path(row["workspace_root_norm"]),
            project_id=project_id,
            workspace_id=workspace_id,
            workspace_root_norm=row["workspace_root_norm"],
            expected_repository_identity_hash=_repository_identity(row),
            workspace_binding_generation=workspace_binding_generation,
        ):
            raise LifecycleOperationError(
                "Live repository identity does not match the workspace binding.",
                code="WORKSPACE_BINDING_CHANGED",
                details={"workspace_id": workspace_id},
            )
        return row

    def _binding_matches(self, reservation: OperationReservation) -> bool:
        return workspace_matches_repository(
            registry=self.registry,
            repository_root=Path(reservation.repository_root_norm),
            project_id=reservation.project_id,
            workspace_id=reservation.workspace_id,
            workspace_root_norm=reservation.repository_root_norm,
            expected_repository_identity_hash=reservation.repository_identity_hash,
            workspace_binding_generation=reservation.workspace_binding_generation,
        )

    def _require_git_state(
        self,
        observation: GitWorkspaceObservation,
        *,
        expected_head: str | None,
        expected_head_ref: str,
        require_clean: bool,
    ) -> None:
        if observation.head is None:
            raise LifecycleOperationError(
                "Git HEAD does not resolve to a commit.",
                code="GIT_HEAD_UNRESOLVED",
            )
        if observation.head != expected_head:
            raise LifecycleOperationError(
                "Live Git HEAD does not match the task expectation.",
                code="GIT_HEAD_CHANGED",
                details={"expected": expected_head, "actual": observation.head},
            )
        if observation.head_ref != expected_head_ref:
            raise LifecycleOperationError(
                "Live Git head ref does not match the task expectation.",
                code="GIT_HEAD_REF_CHANGED",
                details={"expected": expected_head_ref, "actual": observation.head_ref},
            )
        if observation.operations:
            raise LifecycleOperationError(
                "Git has an unresolved merge/rebase/sequencer/bisect operation.",
                code="GIT_OPERATION_IN_PROGRESS",
                details={"operations": list(observation.operations)},
            )
        if observation.conflict_paths:
            raise LifecycleOperationError(
                "Git has unresolved conflicts.",
                code="GIT_CONFLICTS_PRESENT",
                details={"paths": list(observation.conflict_paths)},
            )
        if require_clean and (
            observation.staged_paths
            or observation.unstaged_tracked_paths
            or observation.untracked_paths
        ):
            raise LifecycleOperationError(
                "BEGIN_TASK requires a clean committed workspace.",
                code="WORKSPACE_NOT_CLEAN",
                details={
                    "staged": list(observation.staged_paths),
                    "unstaged_tracked": list(observation.unstaged_tracked_paths),
                    "untracked_non_ignored": list(observation.untracked_paths),
                },
            )

    def _require_no_open_lineage(self, workspace_id: str, binding: str) -> None:
        with self._registry() as conn:
            row = conn.execute(
                """SELECT task_id, task_state FROM lifecycle_tasks
                   WHERE workspace_id = ? AND workspace_binding_generation = ?
                     AND task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED') LIMIT 1""",
                (workspace_id, binding),
            ).fetchone()
        if row is not None:
            raise LifecycleOperationError(
                "Workspace binding already owns an open task lineage.",
                code="OPEN_TASK_EXISTS",
                details={"task_id": row["task_id"], "task_state": row["task_state"]},
            )

    def _project_baseline(self, project_id: str) -> tuple[str, int] | None:
        with self._registry() as conn:
            row = conn.execute(
                """SELECT snapshot_id, pointer_version FROM managed_pointers
                   WHERE project_id = ? AND task_id IS NULL
                     AND pointer_role = 'PROJECT_BASELINE'""",
                (project_id,),
            ).fetchone()
        return None if row is None else (row["snapshot_id"], row["pointer_version"])

    def _require_matching_operation(
        self,
        request: GenerationReservationRequest,
        *,
        operation_kind: str,
        invocation_fingerprint: str,
    ) -> None:
        actor = request.actor_context
        if (
            request.operation_kind != operation_kind
            or actor.get("request_fingerprint") != invocation_fingerprint
        ):
            raise LifecycleOperationError(
                "Idempotency key is already bound to a different lifecycle request.",
                code="IDEMPOTENCY_KEY_REUSED",
                details={"idempotency_key": request.idempotency_key},
            )

    def _classify_operation_failure(
        self,
        reservation: OperationReservation,
        error: ProjectKbError,
        *,
        abandon_draft: bool,
    ) -> None:
        artifact_evidence = any(
            os.path.lexists(path) for path in _operation_artifact_paths(reservation)
        )
        with self._registry() as conn, immediate_registry_transaction(conn):
            operation = conn.execute(
                "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
                (reservation.operation_id,),
            ).fetchone()
            if operation is None or operation["operation_phase"] == "COMMITTED":
                return
            generation = conn.execute(
                "SELECT snapshot_id FROM snapshot_generations WHERE snapshot_id = ?",
                (reservation.snapshot_id,),
            ).fetchone()
            pointer = conn.execute(
                "SELECT pointer_id FROM managed_pointers WHERE snapshot_id = ? LIMIT 1",
                (reservation.snapshot_id,),
            ).fetchone()
            phase = operation["operation_phase"]
            failure = _canonical_json({"code": error.code, "details": error.details})
            timestamp = self._timestamp()
            if phase in {"RESERVED", "BUILDING"} and (
                artifact_evidence or generation is not None or pointer is not None
            ):
                conn.execute(
                    """UPDATE lifecycle_operations
                       SET operation_phase = 'RECOVERY_REQUIRED', failure_json = ?,
                           row_version = row_version + 1, updated_at = ?
                       WHERE operation_id = ? AND row_version = ?""",
                    (failure, timestamp, reservation.operation_id, operation["row_version"]),
                )
                return
            prepublication_terminal = (
                not artifact_evidence
                and generation is None
                and pointer is None
                and phase in {"RESERVED", "BUILDING", "FAILED"}
            )
            if phase in {"RESERVED", "BUILDING"} and prepublication_terminal:
                conn.execute(
                    """UPDATE lifecycle_operations
                       SET operation_phase = 'FAILED', failure_json = ?,
                           row_version = row_version + 1, updated_at = ?
                       WHERE operation_id = ? AND row_version = ?""",
                    (failure, timestamp, reservation.operation_id, operation["row_version"]),
                )
                phase = "FAILED"
            if abandon_draft and phase == "FAILED" and prepublication_terminal:
                task = conn.execute(
                    "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
                    (reservation.task_id,),
                ).fetchone()
                if task is not None and task["task_state"] == "DRAFT":
                    conn.execute(
                        """UPDATE lifecycle_tasks
                           SET task_state = 'ABANDONED', row_version = row_version + 1,
                               updated_at = ? WHERE task_id = ? AND row_version = ?""",
                        (timestamp, reservation.task_id, task["row_version"]),
                    )
                    self._append_event(
                        conn,
                        project_id=reservation.project_id,
                        event_type="lifecycle_begin_failed_before_baseline",
                        message="DRAFT task abandoned after terminal pre-publication failure.",
                        details={
                            "task_id": reservation.task_id,
                            "operation_id": reservation.operation_id,
                            "reason": "BEGIN_FAILED_BEFORE_BASELINE",
                            "failure_code": error.code,
                        },
                    )

    def _mark_task_recovery_required(
        self,
        reservation: OperationReservation,
        error: ProjectKbError,
    ) -> bool:
        failure = _canonical_json(
            {
                "code": error.code,
                "details": error.details,
                "reason": "TASK_RECOVERY_PRECONDITION_MISMATCH",
            }
        )
        with self._registry() as conn, immediate_registry_transaction(conn):
            operation = conn.execute(
                "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
                (reservation.operation_id,),
            ).fetchone()
            if operation is None or operation["operation_phase"] in _TERMINAL_PHASES:
                return False
            if operation["operation_phase"] not in _UNRESOLVED_PHASES:
                raise LifecycleOperationError(
                    "Task recovery mismatch could not be persisted safely.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={
                        "operation_id": reservation.operation_id,
                        "operation_phase": operation["operation_phase"],
                    },
                )
            cursor = conn.execute(
                """UPDATE lifecycle_operations
                   SET operation_phase = 'RECOVERY_REQUIRED', failure_json = ?,
                       row_version = row_version + 1, updated_at = ?
                   WHERE operation_id = ? AND row_version = ?""",
                (
                    failure,
                    self._timestamp(),
                    reservation.operation_id,
                    operation["row_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise LifecycleOperationError(
                    "Task recovery mismatch lost its operation-state compare-and-swap.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    retryable=True,
                    details={"operation_id": reservation.operation_id},
                )
            return True

    def _decorate_error(
        self,
        error: ProjectKbError,
        idempotency_key: str,
    ) -> LifecycleOperationError:
        details = dict(error.details)
        details.update(self._operation_flags(idempotency_key, error.code))
        return LifecycleOperationError(
            error.message,
            code=error.code,
            retryable=error.retryable,
            details=details,
        )

    def _operation_flags(self, idempotency_key: str, error_code: str) -> dict[str, Any]:
        with self._registry() as conn:
            operation = conn.execute(
                "SELECT * FROM lifecycle_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if operation is None:
                return {
                    "writes_may_resume": True,
                    "recovery_required": False,
                    "file_published": False,
                    "generation_registered": False,
                    "pointer_updated": False,
                }
            generation = conn.execute(
                "SELECT snapshot_id FROM snapshot_generations WHERE snapshot_id = ?",
                (operation["reserved_snapshot_id"],),
            ).fetchone()
            pointer = conn.execute(
                "SELECT pointer_id FROM managed_pointers WHERE snapshot_id = ? LIMIT 1",
                (operation["reserved_snapshot_id"],),
            ).fetchone()
        final_path = self.home / operation["managed_final_path"]
        phase = operation["operation_phase"]
        return {
            "operation_id": operation["operation_id"],
            "operation_phase": phase,
            "writes_may_resume": phase in _TERMINAL_PHASES,
            "recovery_required": (
                phase == "RECOVERY_REQUIRED" or error_code == "OPERATION_RECOVERY_REQUIRED"
            ),
            "file_published": os.path.lexists(final_path),
            "generation_registered": generation is not None,
            "pointer_updated": pointer is not None,
        }

    def _pointer_row(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        role: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM managed_pointers WHERE task_id = ? AND pointer_role = ?",
            (task_id, role),
        ).fetchone()

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        event_type: str,
        message: str,
        details: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO registry_events (
                   event_id, project_id, project_name, event_type,
                   message, details_json, created_at
               ) VALUES (?, ?, NULL, ?, ?, ?, ?)""",
            (
                self._new_id(),
                project_id,
                event_type,
                message,
                _canonical_json(details),
                self._timestamp(),
            ),
        )

    @contextmanager
    def _registry(self) -> Iterator[sqlite3.Connection]:
        with open_existing_registry(
            home=self.home,
            now=self._timestamp,
            event_id=self._new_id,
        ) as connection:
            if connection is None:
                raise LifecycleOperationError(
                    "Registry does not exist for lifecycle operation.",
                    code="REGISTRY_NOT_FOUND",
                )
            yield connection

    def _default_lease(self, actor: ActorContext, idempotency_key: str) -> OperationLease:
        del idempotency_key
        return OperationLease(
            owner=f"{actor.kind.value}:{actor.subject}",
            token=uuid.uuid4().hex,
        )

    def _new_id(self) -> str:
        value = self._id_factory()
        _require_id(value, "generated_id")
        return value

    def _timestamp(self) -> str:
        return self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_begin_request(request: BeginTaskRequest) -> None:
    _validate_idempotency_key(request.idempotency_key)
    for field, value in (
        ("project_id", request.project_id),
        ("workspace_id", request.workspace_id),
        ("workspace_binding_generation", request.workspace_binding_generation),
    ):
        _require_id(value, field)
    if not _HEAD_PATTERN.fullmatch(request.expected_git_head):
        raise LifecycleOperationError(
            "Expected Git HEAD must be a full commit object ID.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "expected_git_head"},
        )
    if not _is_supported_head_ref(request.expected_head_ref):
        raise LifecycleOperationError(
            "Expected head ref must be a supported full branch ref or explicit DETACHED.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "expected_head_ref"},
        )


def _validate_refresh_request(request: RefreshWorkingRequest) -> None:
    _validate_idempotency_key(request.idempotency_key)
    if request.write_pause_acknowledged is not True:
        raise LifecycleOperationError(
            "REFRESH_WORKING requires explicit write-pause acknowledgement.",
            code="WRITE_PAUSE_REQUIRED",
            details={"write_pause_acknowledged": request.write_pause_acknowledged},
        )
    if not isinstance(request.task_context, TaskContext):
        raise LifecycleOperationError(
            "REFRESH_WORKING requires an explicit TaskContext.",
            code="TASK_CONTEXT_REQUIRED",
        )


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LifecycleOperationError(
            "Idempotency key must contain 1-256 characters.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "idempotency_key"},
        )


def _require_actor(actor: ActorContext, allowed: frozenset[ActorKind]) -> None:
    if (
        not isinstance(actor, ActorContext)
        or not isinstance(actor.kind, ActorKind)
        or actor.kind not in allowed
        or not isinstance(actor.subject, str)
        or not actor.subject
        or not isinstance(actor.invocation_id, str)
        or not actor.invocation_id
    ):
        raise LifecycleOperationError(
            "Invocation-bound actor is not authorized for this lifecycle surface.",
            code="ACTOR_NOT_AUTHORIZED",
        )


def _operation_actor_context(actor: ActorContext, request_fingerprint: str) -> dict[str, str]:
    return {
        "actor_kind": actor.kind.value,
        "actor_subject": actor.subject,
        "invocation_id": actor.invocation_id,
        "request_fingerprint": request_fingerprint,
    }


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise LifecycleOperationError(
            "Lifecycle identifier is invalid.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": field},
        )
    return value


def _stored_head_ref(value: str) -> str | None:
    return None if value == DETACHED_HEAD_REF else value


def _public_head_ref(value: object) -> str:
    return DETACHED_HEAD_REF if value is None else str(value)


def _is_supported_head_ref(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value == DETACHED_HEAD_REF:
        return True
    if not value.startswith(_FULL_HEAD_REF_PREFIX):
        return False
    branch = value.removeprefix(_FULL_HEAD_REF_PREFIX)
    if (
        not branch
        or branch == "@"
        or branch.endswith(("/", "."))
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character in _FORBIDDEN_HEAD_REF_CHARACTERS
            for character in branch
        )
    ):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith((".", ".lock"))
        for component in branch.split("/")
    )


def _operation_artifact_paths(reservation: OperationReservation) -> tuple[Path, ...]:
    temp = reservation.paths.temp_path
    final = reservation.paths.final_path
    return (
        temp,
        Path(f"{temp}-journal"),
        Path(f"{temp}-wal"),
        Path(f"{temp}-shm"),
        final,
        Path(f"{final}-journal"),
        Path(f"{final}-wal"),
        Path(f"{final}-shm"),
    )


def _repository_identity(workspace: sqlite3.Row) -> str:
    from project_kb.resolver.repo_identity import repository_identity_hash

    return repository_identity_hash(
        workspace["workspace_root_norm"],
        workspace["repository_fingerprint_json"],
    )


def _json_object(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise LifecycleOperationError(
            "Persisted lifecycle JSON evidence is invalid.",
            code="OPERATION_RECOVERY_REQUIRED",
        ) from exc
    if not isinstance(result, dict):
        raise LifecycleOperationError(
            "Persisted lifecycle JSON evidence is not an object.",
            code="OPERATION_RECOVERY_REQUIRED",
        )
    return result


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
