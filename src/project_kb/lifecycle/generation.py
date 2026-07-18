"""File-first immutable generation publication and operation-local recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from project_kb.errors import (
    LifecycleOperationError,
    RegistryOperationError,
    SnapshotQueryError,
)
from project_kb.indexing.capture import (
    CaptureContract,
    SealedArtifactDescriptor,
)
from project_kb.registry.db import (
    immediate_registry_transaction,
    open_existing_registry,
)
from project_kb.registry.service import _compare_and_swap_pointer
from project_kb.resolver.repo_identity import repository_identity_hash
from project_kb.snapshot.database import SCHEMA_VERSION as SNAPSHOT_SCHEMA_VERSION
from project_kb.snapshot.database import validate_snapshot
from project_kb.storage.home import (
    GENERATION_STORAGE_LAYOUT_VERSION,
    ManagedGenerationPaths,
    managed_generation_paths,
    prepare_managed_generation_storage,
    require_managed_regular_file,
    resolve_managed_generation_file,
)

_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_UNRESOLVED_PHASES = frozenset({"RESERVED", "BUILDING", "FILE_PUBLISHED", "RECOVERY_REQUIRED"})
_TERMINAL_PHASES = frozenset({"COMMITTED", "FAILED"})
_OPERATION_SPECS = {
    "BEGIN_TASK": ("TASK_BASELINE", "TASK_BASELINE", "DRAFT"),
    "REFRESH_WORKING": ("TASK_WORKING", "TASK_LATEST_WORKING", "ACTIVE"),
    "ACCEPT_TASK": ("TASK_FINAL", "TASK_FINAL", "ACTIVE"),
}
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_SEALED_EVENT_TYPE = "lifecycle_generation_sealed"
_CAPTURE_CONTRACT_FINGERPRINT_VERSION = 1
_WINDOWS_RENAME_FAILS_IF_EXISTS = os.name == "nt"
_TERMINAL_PREPUBLICATION_CODES = frozenset(
    {
        "POINTER_CAS_CONFLICT",
        "TASK_VERSION_CONFLICT",
        "TASK_STATE_CONFLICT",
        "TASK_CAPTURE_CONTRACT_INVALID",
        "WORKSPACE_BINDING_CONFLICT",
        "GENERATION_LINEAGE_INVALID",
        "GENERATION_PARENT_INVALID",
        "GENERATION_BROKEN",
        "BROKEN_POINTER",
    }
)


@dataclass(frozen=True)
class OperationLease:
    """Caller-bound lease evidence for one operation attempt."""

    owner: str
    token: str
    duration_seconds: int = 300


@dataclass(frozen=True)
class PointerExpectation:
    """One exact task-pointer creation or forward-CAS expectation."""

    pointer_id: str
    pointer_role: str
    expected_pointer_version: int | None
    expected_snapshot_id: str | None


@dataclass(frozen=True)
class GenerationReservationRequest:
    """Normalized internal request for a generation-producing operation."""

    idempotency_key: str
    operation_kind: str
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    task_id: str
    expected_task_version: int
    parent_snapshot_id: str | None
    pointer_expectations: tuple[PointerExpectation, ...]
    expected_head: str | None
    expected_branch: str | None
    actor_context: Mapping[str, Any]
    activate_task_on_commit: bool = False


@dataclass(frozen=True)
class BeginTaskReservation:
    """Task facts inserted atomically with a new BEGIN_TASK reservation."""

    capture_contract_fingerprint: str
    baseline_head_commit: str
    baseline_head_ref: str | None
    project_baseline_snapshot_id: str | None
    project_baseline_pointer_version: int | None


@dataclass(frozen=True)
class OperationReservation:
    """Durable reservation returned to the capture/publication orchestrator."""

    operation_id: str
    request_fingerprint: str
    operation_kind: str
    operation_phase: str
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    task_id: str
    snapshot_id: str
    generation_sequence: int
    capture_purpose: str
    capture_contract_fingerprint: str
    repository_root_norm: str
    repository_identity_hash: str
    parent_snapshot_id: str | None
    pointer_expectations: tuple[PointerExpectation, ...]
    predecessor_pointer: PointerExpectation | None
    expected_task_version: int
    reserved_task_version: int
    expected_head: str | None
    expected_branch: str | None
    activate_task_on_commit: bool
    paths: ManagedGenerationPaths
    lease_owner: str
    lease_token: str
    lease_expires_at: str
    replayed: bool = False
    lease_reclaimed: bool = False


@dataclass(frozen=True)
class GenerationDescriptor:
    """Exact verified immutable generation returned by the internal resolver."""

    snapshot_id: str
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    task_id: str
    generation_sequence: int
    capture_purpose: str
    origin_operation_id: str
    generation_state: str
    relative_storage_path: str
    path: Path
    file_size: int
    file_sha256: str
    schema_version: int


@dataclass(frozen=True)
class GenerationPublicationResult:
    """Stable result for a committed or replayed publication operation."""

    operation_id: str
    snapshot_id: str
    generation_sequence: int
    generation_state: str
    relative_storage_path: str
    file_size: int
    file_sha256: str
    pointer_versions: tuple[tuple[str, int], ...]
    task_state: str
    task_row_version: int
    replayed: bool


@dataclass(frozen=True)
class _VerifiedArtifact:
    metadata: dict[str, Any]
    file_size: int
    file_sha256: str
    repository_evidence_json: str


@dataclass(frozen=True)
class _LineageWitness:
    snapshot_id: str
    generation_sequence: int
    capture_purpose: str
    origin_operation_id: str
    generation_row_version: int
    file_size: int
    file_sha256: str
    pointer_id: str
    pointer_role: str
    pointer_version: int


class _FinalizationConflict(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def capture_contract_fingerprint(contract: CaptureContract) -> str:
    """Return the stable fingerprint persisted by a lifecycle task."""

    payload = {
        "fingerprint_version": _CAPTURE_CONTRACT_FINGERPRINT_VERSION,
        "schema_version": contract.schema_version,
        "scanner_version": contract.scanner_version,
        "policy": asdict(contract.policy),
        "extractor_versions": list(contract.extractor_versions),
        "proof_contract_version": contract.proof_contract_version,
        "verifier_version": contract.verifier_version,
        "module_map_version": contract.module_map_version,
        "occurrence_contract_version": contract.occurrence_contract_version,
        "generation_storage_layout_version": GENERATION_STORAGE_LAYOUT_VERSION,
        "capture_scope_exclusion_contract": {
            "policy_version": contract.policy.policy_version,
            "discovered_metadata_paths": list(contract.policy.discovered_metadata_paths),
            "discovered_pruned_roots": list(contract.policy.discovered_pruned_roots),
        },
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class LifecycleGenerationService:
    """Internal owned facade for one exact immutable-generation operation."""

    def __init__(
        self,
        *,
        home: Path,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.home = home
        self._clock = clock or _utc_clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._checkpoint_callback = checkpoint

    def reserve_operation(
        self,
        request: GenerationReservationRequest,
        lease: OperationLease,
    ) -> OperationReservation:
        """Reserve identity/sequence/CAS evidence in one short immediate transaction."""

        return self._reserve_operation(request, lease, begin_task=None)

    def reserve_begin_task_operation(
        self,
        request: GenerationReservationRequest,
        lease: OperationLease,
        begin_task: BeginTaskReservation,
    ) -> OperationReservation:
        """Create one DRAFT and reserve its baseline operation atomically."""

        if request.operation_kind != "BEGIN_TASK" or not request.activate_task_on_commit:
            raise LifecycleOperationError(
                "Atomic task creation is available only to an activating BEGIN_TASK request.",
                code="LIFECYCLE_REQUEST_INVALID",
            )
        return self._reserve_operation(request, lease, begin_task=begin_task)

    def _reserve_operation(
        self,
        request: GenerationReservationRequest,
        lease: OperationLease,
        *,
        begin_task: BeginTaskReservation | None,
    ) -> OperationReservation:
        """Owned implementation shared by existing-task and atomic-begin reservations."""

        _validate_reservation_request(request)
        _validate_lease(lease)
        normalized_request = _request_plan(request)
        request_fingerprint = _fingerprint(normalized_request)
        lease_json = _lease_json(lease)
        lease_expires_at = _iso_timestamp(self._now() + timedelta(seconds=lease.duration_seconds))

        with self._registry() as conn:
            preview = conn.execute(
                "SELECT operation_id FROM lifecycle_operations WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            lineage_witness = None if preview is not None else self._preflight_lineage(request)
            lease_reclaimed = False
            with immediate_registry_transaction(conn):
                existing = conn.execute(
                    "SELECT * FROM lifecycle_operations WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != request_fingerprint:
                        raise LifecycleOperationError(
                            "The idempotency key is already bound to a different request.",
                            code="IDEMPOTENCY_KEY_REUSED",
                            details={
                                "idempotency_key": request.idempotency_key,
                                "operation_id": existing["operation_id"],
                            },
                        )
                    if existing["operation_phase"] in _UNRESOLVED_PHASES:
                        owner, token = _lease_parts(existing["lease_owner"])
                        expires_at = _parse_timestamp(existing["lease_expires_at"])
                        same_lease = owner == lease.owner and token == lease.token
                        lease_active = expires_at > self._now()
                        if not same_lease and lease_active:
                            raise LifecycleOperationError(
                                "The exact lifecycle operation is already in progress.",
                                code="OPERATION_IN_PROGRESS",
                                retryable=True,
                                details={
                                    "operation_id": existing["operation_id"],
                                    "snapshot_id": existing["reserved_snapshot_id"],
                                    "lease_expires_at": existing["lease_expires_at"],
                                },
                            )
                        if not lease_active:
                            cursor = conn.execute(
                                """UPDATE lifecycle_operations
                                   SET lease_owner = ?, lease_expires_at = ?,
                                       row_version = row_version + 1, updated_at = ?
                                   WHERE operation_id = ? AND row_version = ?""",
                                (
                                    lease_json,
                                    lease_expires_at,
                                    self._timestamp(),
                                    existing["operation_id"],
                                    existing["row_version"],
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise LifecycleOperationError(
                                    "Operation lease compare-and-swap failed.",
                                    code="OPERATION_LEASE_CONFLICT",
                                    retryable=True,
                                    details={"operation_id": existing["operation_id"]},
                                )
                            lease_reclaimed = True
                            existing = conn.execute(
                                "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
                                (existing["operation_id"],),
                            ).fetchone()
                    reservation = self._reservation_from_row(
                        conn,
                        existing,
                        replayed=existing["operation_phase"] == "COMMITTED",
                        lease_reclaimed=lease_reclaimed,
                    )
                else:
                    workspace = self._require_workspace(conn, request)
                    if begin_task is not None:
                        self._insert_begin_task(conn, request, begin_task)
                    task = self._require_task_for_reservation(conn, request)
                    unresolved = conn.execute(
                        """SELECT operation_id, reserved_snapshot_id, operation_phase
                           FROM lifecycle_operations
                           WHERE task_id = ?
                             AND operation_phase IN (
                                 'RESERVED', 'BUILDING', 'FILE_PUBLISHED',
                                 'RECOVERY_REQUIRED'
                             )
                           LIMIT 1""",
                        (request.task_id,),
                    ).fetchone()
                    if unresolved is not None:
                        raise LifecycleOperationError(
                            "Task already owns an unresolved generation operation.",
                            code="OPERATION_IN_PROGRESS",
                            retryable=True,
                            details={
                                "operation_id": unresolved["operation_id"],
                                "snapshot_id": unresolved["reserved_snapshot_id"],
                                "operation_phase": unresolved["operation_phase"],
                            },
                        )
                    self._require_pointer_preconditions(
                        conn,
                        request,
                        task_id=request.task_id,
                        finalizing=False,
                    )
                    sequence = task["next_generation_sequence"]
                    if lineage_witness is None and request.operation_kind != "BEGIN_TASK":
                        raise LifecycleOperationError(
                            "Reservation lost its validated lineage preflight.",
                            code="GENERATION_LINEAGE_INVALID",
                            details={"task_id": request.task_id},
                        )
                    self._require_generation_shape(
                        conn,
                        request,
                        sequence,
                        lineage_witness=lineage_witness,
                    )
                    operation_id = self._new_id()
                    snapshot_id = self._new_id()
                    paths = managed_generation_paths(
                        self.home,
                        snapshot_id=snapshot_id,
                        operation_id=operation_id,
                    )
                    _require_generation_storage_isolated(
                        paths,
                        repository_root=workspace["workspace_root_norm"],
                    )
                    durable_plan = _persisted_operation_plan(
                        normalized_request,
                        capture_contract_fingerprint=task["capture_contract_fingerprint"],
                        repository_root_norm=workspace["workspace_root_norm"],
                        repository_identity_hash_value=repository_identity_hash(
                            workspace["workspace_root_norm"],
                            workspace["repository_fingerprint_json"],
                        ),
                        predecessor_pointer=(
                            None
                            if lineage_witness is None
                            else PointerExpectation(
                                pointer_id=lineage_witness.pointer_id,
                                pointer_role=lineage_witness.pointer_role,
                                expected_pointer_version=lineage_witness.pointer_version,
                                expected_snapshot_id=lineage_witness.snapshot_id,
                            )
                        ),
                    )
                    cursor = conn.execute(
                        """UPDATE lifecycle_tasks
                           SET next_generation_sequence = next_generation_sequence + 1,
                               row_version = row_version + 1, updated_at = ?
                           WHERE task_id = ? AND row_version = ?
                             AND task_state = ? AND next_generation_sequence = ?""",
                        (
                            self._timestamp(),
                            request.task_id,
                            request.expected_task_version,
                            _OPERATION_SPECS[request.operation_kind][2],
                            sequence,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise LifecycleOperationError(
                            "Task reservation compare-and-swap failed.",
                            code="TASK_VERSION_CONFLICT",
                            details={
                                "task_id": request.task_id,
                                "expected_task_version": request.expected_task_version,
                            },
                        )
                    conn.execute(
                        """INSERT INTO lifecycle_operations (
                               operation_id, idempotency_key, request_fingerprint,
                               operation_kind, operation_phase, actor_context_json,
                               project_id, workspace_id, workspace_binding_generation, task_id,
                               reserved_snapshot_id, reserved_generation_sequence,
                               expected_task_version, expected_pointer_versions_json,
                               lease_owner, lease_expires_at, managed_temp_path,
                               managed_final_path, failure_json, row_version,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                     ?, ?, NULL, 0, ?, ?)""",
                        (
                            operation_id,
                            request.idempotency_key,
                            request_fingerprint,
                            request.operation_kind,
                            _canonical_json(dict(request.actor_context)),
                            request.project_id,
                            request.workspace_id,
                            request.workspace_binding_generation,
                            request.task_id,
                            snapshot_id,
                            sequence,
                            request.expected_task_version,
                            _canonical_json(durable_plan),
                            lease_json,
                            lease_expires_at,
                            paths.relative_temp_path,
                            paths.relative_final_path,
                            self._timestamp(),
                            self._timestamp(),
                        ),
                    )
                    self._append_event(
                        conn,
                        project_id=request.project_id,
                        event_type="lifecycle_generation_reserved",
                        message="Lifecycle generation operation reserved.",
                        details={
                            "operation_id": operation_id,
                            "snapshot_id": snapshot_id,
                            "task_id": request.task_id,
                            "generation_sequence": sequence,
                        },
                    )
                    row = conn.execute(
                        "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    reservation = self._reservation_from_row(conn, row)
            return reservation

    def begin_build(self, reservation: OperationReservation) -> OperationReservation:
        """Prepare managed scratch storage and persist BUILDING before capture starts."""

        canonical = self._canonical_caller_reservation(reservation)
        lineage_witness = self._preflight_lineage(
            _request_from_reservation(canonical),
            expected_sequence=canonical.generation_sequence,
            expected_predecessor=canonical.predecessor_pointer,
        )
        with self._registry() as conn, immediate_registry_transaction(conn):
            row = self._require_operation(conn, canonical.operation_id)
            canonical = self._require_reservation_identity(conn, row, canonical)
            self._require_active_lease(row, canonical)
            self._require_current_binding(conn, canonical)
            self._require_task_finalization_preconditions(
                conn,
                canonical,
                finalizing=False,
            )
            request = _request_from_reservation(canonical)
            self._require_pointer_preconditions(
                conn,
                request,
                task_id=canonical.task_id,
                finalizing=False,
            )
            self._require_generation_shape(
                conn,
                request,
                canonical.generation_sequence,
                lineage_witness=lineage_witness,
                expected_predecessor=canonical.predecessor_pointer,
            )
            if row["operation_phase"] == "RESERVED":
                self._update_operation_phase(
                    conn,
                    row,
                    phase="BUILDING",
                )
                self._append_event(
                    conn,
                    project_id=canonical.project_id,
                    event_type="lifecycle_generation_building",
                    message="Lifecycle generation build started.",
                    details={"operation_id": canonical.operation_id},
                )
            elif row["operation_phase"] != "BUILDING":
                raise LifecycleOperationError(
                    "Operation cannot enter the build phase from its current state.",
                    code="OPERATION_PHASE_CONFLICT",
                    details={
                        "operation_id": canonical.operation_id,
                        "operation_phase": row["operation_phase"],
                    },
                )
        prepare_managed_generation_storage(canonical.paths)
        return self._load_reservation(canonical.operation_id)

    def publish_artifact(
        self,
        reservation: OperationReservation,
        descriptor: SealedArtifactDescriptor,
    ) -> GenerationPublicationResult:
        """Publish, revalidate, register, CAS, and complete one reserved operation."""

        canonical = self._canonical_caller_reservation(reservation)
        return self._publish_reserved(canonical, descriptor=descriptor)

    def recover_operation(
        self,
        request: GenerationReservationRequest,
        lease: OperationLease,
        *,
        descriptor: SealedArtifactDescriptor | None = None,
    ) -> GenerationPublicationResult:
        """Reconcile only the exact operation identified by this idempotent request."""

        reservation = self.reserve_operation(request, lease)
        if reservation.operation_phase == "COMMITTED":
            return self._committed_result(reservation, replayed=True)
        if reservation.operation_phase == "FAILED":
            raise self._failed_operation_error(reservation.operation_id)

        generation_row = self._generation_row(reservation.snapshot_id)
        if generation_row is not None:
            state = generation_row["generation_state"]
            if state == "AVAILABLE":
                return self._committed_result(reservation, replayed=True)
            raise LifecycleOperationError(
                "The exact operation already produced a non-available generation.",
                code="OPERATION_GENERATION_NOT_AVAILABLE",
                details={
                    "operation_id": reservation.operation_id,
                    "snapshot_id": reservation.snapshot_id,
                    "generation_state": state,
                },
            )

        if os.path.lexists(reservation.paths.final_path):
            witness = self._sealed_witness(reservation)
            if witness is None:
                self._mark_recovery_required(
                    reservation.operation_id,
                    code="SEALED_WITNESS_MISSING",
                    details={"relative_path": reservation.paths.relative_final_path},
                )
                raise LifecycleOperationError(
                    "Published generation has no durable operation-local seal witness.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={
                        "operation_id": reservation.operation_id,
                        "snapshot_id": reservation.snapshot_id,
                    },
                )
            try:
                verified = self._verify_artifact(
                    reservation.paths.final_path,
                    reservation,
                    descriptor=descriptor,
                    compute_hash=True,
                )
                self._require_witness_match(witness, verified, reservation)
            except LifecycleOperationError as exc:
                self._mark_recovery_required(
                    reservation.operation_id,
                    code="FINAL_FILE_MISMATCH",
                    details={"cause": exc.to_dict()},
                )
                raise LifecycleOperationError(
                    "Published generation evidence is ambiguous or mismatched.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={
                        "operation_id": reservation.operation_id,
                        "snapshot_id": reservation.snapshot_id,
                        "cause": exc.to_dict(),
                    },
                ) from exc
            self._reconcile_duplicate_temp(
                reservation,
                witness=witness,
                final_artifact=verified,
                descriptor=descriptor,
            )
            self._ensure_file_published_phase(reservation)
            return self._finalize_or_orphan(reservation, verified)

        if descriptor is not None:
            return self._publish_reserved(reservation, descriptor=descriptor)

        if os.path.lexists(reservation.paths.temp_path):
            if not reservation.lease_reclaimed:
                raise LifecycleOperationError(
                    "The operation-owned build artifact is still covered by an active lease.",
                    code="OPERATION_IN_PROGRESS",
                    retryable=True,
                    details={"operation_id": reservation.operation_id},
                )
            try:
                validate_snapshot(reservation.paths.temp_path, require_v2=True)
            except SnapshotQueryError as exc:
                if self._cleanup_operation_temp(reservation.paths):
                    self._mark_failed(
                        reservation.operation_id,
                        code="INCOMPLETE_TEMP_ARTIFACT",
                        details={"cause": exc.to_dict()},
                    )
                    raise LifecycleOperationError(
                        "Expired operation owned an incomplete temporary artifact.",
                        code="OPERATION_BUILD_INCOMPLETE",
                        details={"operation_id": reservation.operation_id},
                    ) from exc
                self._mark_recovery_required(
                    reservation.operation_id,
                    code="TEMP_CLEANUP_AMBIGUOUS",
                    details={"cause": exc.to_dict()},
                )
                raise LifecycleOperationError(
                    "Temporary artifact cleanup was not provably safe.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={"operation_id": reservation.operation_id},
                ) from exc
            return self._publish_reserved(reservation, descriptor=None)

        if reservation.lease_reclaimed:
            self._mark_failed(
                reservation.operation_id,
                code="BUILD_ARTIFACT_MISSING",
                details={"temp_path": reservation.paths.relative_temp_path},
            )
            raise LifecycleOperationError(
                "Expired operation has no published or temporary artifact.",
                code="OPERATION_BUILD_INCOMPLETE",
                details={"operation_id": reservation.operation_id},
            )
        raise LifecycleOperationError(
            "The exact lifecycle operation is still in progress.",
            code="OPERATION_IN_PROGRESS",
            retryable=True,
            details={
                "operation_id": reservation.operation_id,
                "operation_phase": reservation.operation_phase,
            },
        )

    def request_for_idempotency_key(
        self,
        idempotency_key: str,
    ) -> GenerationReservationRequest | None:
        """Return the exact durable generation request for task-level replay."""

        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise LifecycleOperationError(
                "Idempotency key is invalid.",
                code="LIFECYCLE_REQUEST_INVALID",
                details={"field": "idempotency_key"},
            )
        with self._registry() as conn:
            row = conn.execute(
                "SELECT * FROM lifecycle_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            durable_plan = _json_object(
                row["expected_pointer_versions_json"],
                field="operation_plan",
            )
            normalized_request = _persisted_request_plan(durable_plan)
            request = _request_from_normalized_plan(idempotency_key, normalized_request)
            if row["request_fingerprint"] != _fingerprint(_request_plan(request)):
                raise LifecycleOperationError(
                    "Persisted request fingerprint does not match its normalized plan.",
                    code="OPERATION_IDENTITY_MISMATCH",
                    details={"operation_id": row["operation_id"]},
                )
            return request

    def resolve_generation(
        self,
        snapshot_id: str,
        *,
        allowed_states: frozenset[str] = frozenset({"AVAILABLE"}),
    ) -> GenerationDescriptor:
        """Resolve and verify one exact registered generation with no selector fallback."""

        _require_id(snapshot_id, field="snapshot_id")
        with self._registry() as conn:
            row = conn.execute(
                "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise LifecycleOperationError(
                    "Exact lifecycle generation is not registered.",
                    code="GENERATION_NOT_FOUND",
                    details={"snapshot_id": snapshot_id},
                )
            if row["generation_state"] not in allowed_states:
                raise LifecycleOperationError(
                    "Generation lifecycle state is not permitted for this caller.",
                    code="GENERATION_STATE_BLOCKED",
                    details={
                        "snapshot_id": snapshot_id,
                        "generation_state": row["generation_state"],
                    },
                )
            generation = dict(row)

        path: Path | None = None
        try:
            path = resolve_managed_generation_file(
                self.home,
                snapshot_id=snapshot_id,
                relative_storage_path=generation["relative_storage_path"],
            )
            require_managed_regular_file(
                path,
                generation_root=managed_generation_paths(
                    self.home,
                    snapshot_id=snapshot_id,
                    operation_id=generation["origin_operation_id"],
                ).generation_root,
            )
        except RegistryOperationError as exc:
            code = "GENERATION_MISSING" if not path_exists_safely(path) else "GENERATION_CORRUPT"
            raise LifecycleOperationError(
                "Registered generation storage is missing or unsafe.",
                code=code,
                details={"snapshot_id": snapshot_id, "cause": exc.to_dict()},
            ) from exc

        sidecars = [
            candidate for candidate in _artifact_paths(path)[1:] if os.path.lexists(candidate)
        ]
        if sidecars:
            raise LifecycleOperationError(
                "Registered generation unexpectedly owns SQLite sidecar files.",
                code="GENERATION_CORRUPT",
                details={
                    "snapshot_id": snapshot_id,
                    "sidecars": [str(candidate) for candidate in sidecars],
                },
            )

        actual_size = path.stat().st_size
        if actual_size != generation["file_size"]:
            raise LifecycleOperationError(
                "Registered generation size does not match immutable metadata.",
                code="GENERATION_CORRUPT",
                details={
                    "snapshot_id": snapshot_id,
                    "expected_size": generation["file_size"],
                    "actual_size": actual_size,
                },
            )
        try:
            metadata = validate_snapshot(
                path,
                project_id=generation["project_id"],
                expected_repo_root_norm=_repository_root_from_evidence(
                    generation["repository_evidence_json"]
                ),
                expected_repository_identity_hash=_repository_identity_from_evidence(
                    generation["repository_evidence_json"]
                ),
                expected_repository_binding_generation=generation["workspace_binding_generation"],
                require_v2=True,
            )
        except SnapshotQueryError as exc:
            raise LifecycleOperationError(
                "Registered generation failed read-only semantic validation.",
                code="GENERATION_CORRUPT",
                details={"snapshot_id": snapshot_id, "cause": exc.to_dict()},
            ) from exc
        if metadata["snapshot_id"] != snapshot_id:
            raise LifecycleOperationError(
                "Registered generation internal identity does not match its registry key.",
                code="GENERATION_CORRUPT",
                details={"snapshot_id": snapshot_id},
            )
        if (
            _contract_fingerprint_from_metadata(metadata)
            != generation["capture_contract_fingerprint"]
        ):
            raise LifecycleOperationError(
                "Registered generation capture contract does not match registry metadata.",
                code="GENERATION_CORRUPT",
                details={"snapshot_id": snapshot_id},
            )
        actual_sha256 = _hash_file(path)
        if actual_sha256 != generation["file_sha256"]:
            raise LifecycleOperationError(
                "Registered generation hash does not match immutable metadata.",
                code="GENERATION_CORRUPT",
                details={"snapshot_id": snapshot_id},
            )
        with self._registry() as conn:
            current = conn.execute(
                """SELECT generation_state, row_version
                   FROM snapshot_generations WHERE snapshot_id = ?""",
                (snapshot_id,),
            ).fetchone()
        if (
            current is None
            or current["generation_state"] != generation["generation_state"]
            or current["row_version"] != generation["row_version"]
        ):
            raise LifecycleOperationError(
                "Generation lifecycle state changed during exact resolution.",
                code="GENERATION_STATE_CHANGED",
                retryable=True,
                details={
                    "snapshot_id": snapshot_id,
                    "original_state": generation["generation_state"],
                    "current_state": current["generation_state"] if current else None,
                },
            )
        return GenerationDescriptor(
            snapshot_id=snapshot_id,
            project_id=generation["project_id"],
            workspace_id=generation["workspace_id"],
            workspace_binding_generation=generation["workspace_binding_generation"],
            task_id=generation["task_id"],
            generation_sequence=generation["generation_sequence"],
            capture_purpose=generation["capture_purpose"],
            origin_operation_id=generation["origin_operation_id"],
            generation_state=generation["generation_state"],
            relative_storage_path=generation["relative_storage_path"],
            path=path,
            file_size=actual_size,
            file_sha256=actual_sha256,
            schema_version=metadata["schema_version"],
        )

    def _publish_reserved(
        self,
        reservation: OperationReservation,
        *,
        descriptor: SealedArtifactDescriptor | None,
    ) -> GenerationPublicationResult:
        reservation = self._require_prepublication_state(reservation)
        prepare_managed_generation_storage(reservation.paths)
        if descriptor is not None and _normalized_path(
            descriptor.artifact_path
        ) != _normalized_path(reservation.paths.temp_path):
            raise LifecycleOperationError(
                "Sealed artifact is not the operation-owned managed temporary file.",
                code="ARTIFACT_OWNERSHIP_MISMATCH",
                details={
                    "operation_id": reservation.operation_id,
                    "artifact_path": str(descriptor.artifact_path),
                },
            )
        sealed = self._verify_artifact(
            reservation.paths.temp_path,
            reservation,
            descriptor=descriptor,
            compute_hash=True,
        )
        witness = self._record_sealed_witness(reservation, sealed)
        self._checkpoint("before_file_publication")
        self._create_once_publish(reservation, descriptor=descriptor)
        self._checkpoint("after_file_publication")
        verified = self._verify_artifact(
            reservation.paths.final_path,
            reservation,
            descriptor=descriptor,
            compute_hash=True,
        )
        self._require_witness_match(witness, verified, reservation)
        self._require_operation_temp_absent(reservation)
        self._ensure_file_published_phase(reservation)
        self._checkpoint("before_registry_finalize")
        result = self._finalize_or_orphan(reservation, verified)
        self._checkpoint("after_registry_commit")
        return result

    def _create_once_publish(
        self,
        reservation: OperationReservation,
        *,
        descriptor: SealedArtifactDescriptor | None,
    ) -> None:
        if not _WINDOWS_RENAME_FAILS_IF_EXISTS:
            raise LifecycleOperationError(
                "The probed create-once rename primitive is unavailable on this platform.",
                code="PUBLICATION_PLATFORM_UNSUPPORTED",
                details={"platform": os.name},
            )
        require_managed_regular_file(
            reservation.paths.temp_path,
            generation_root=reservation.paths.generation_root,
        )
        source_device = reservation.paths.temp_path.stat().st_dev
        destination_device = reservation.paths.final_path.parent.stat().st_dev
        if source_device != destination_device:
            raise LifecycleOperationError(
                "Lifecycle publication requires source and destination on one managed volume.",
                code="PUBLICATION_CROSS_VOLUME",
                details={"operation_id": reservation.operation_id},
            )
        try:
            os.rename(reservation.paths.temp_path, reservation.paths.final_path)
        except OSError as exc:
            if os.path.lexists(reservation.paths.final_path):
                self._reconcile_existing_destination(
                    reservation,
                    descriptor=descriptor,
                    publication_error=exc,
                )
                return
            if isinstance(exc, PermissionError):
                raise LifecycleOperationError(
                    "Generation source is locked against create-once publication.",
                    code="PUBLICATION_SHARING_VIOLATION",
                    retryable=True,
                    details={
                        "operation_id": reservation.operation_id,
                        "winerror": getattr(exc, "winerror", None),
                    },
                ) from exc
            raise LifecycleOperationError(
                "Create-once generation publication failed.",
                code="PUBLICATION_FAILED",
                retryable=True,
                details={
                    "operation_id": reservation.operation_id,
                    "error_type": type(exc).__name__,
                    "winerror": getattr(exc, "winerror", None),
                },
            ) from exc

    def _reconcile_existing_destination(
        self,
        reservation: OperationReservation,
        *,
        descriptor: SealedArtifactDescriptor | None,
        publication_error: OSError,
    ) -> None:
        witness = self._sealed_witness(reservation)
        if witness is None:
            self._mark_recovery_required(
                reservation.operation_id,
                code="SEALED_WITNESS_MISSING",
                details={"relative_path": reservation.paths.relative_final_path},
            )
            raise LifecycleOperationError(
                "Existing generation destination has no durable seal witness.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={"operation_id": reservation.operation_id},
            ) from publication_error
        try:
            destination = self._verify_artifact(
                reservation.paths.final_path,
                reservation,
                descriptor=descriptor,
                compute_hash=True,
            )
            self._require_witness_match(witness, destination, reservation)
        except LifecycleOperationError as verify_exc:
            self._mark_recovery_required(
                reservation.operation_id,
                code="PUBLICATION_COLLISION",
                details={"cause": verify_exc.to_dict()},
            )
            raise LifecycleOperationError(
                "Existing generation destination is not the proven operation artifact.",
                code="PUBLICATION_COLLISION",
                details={"operation_id": reservation.operation_id},
            ) from verify_exc
        self._reconcile_duplicate_temp(
            reservation,
            witness=witness,
            final_artifact=destination,
            descriptor=descriptor,
        )

    def _reconcile_duplicate_temp(
        self,
        reservation: OperationReservation,
        *,
        witness: Mapping[str, Any],
        final_artifact: _VerifiedArtifact,
        descriptor: SealedArtifactDescriptor | None,
    ) -> None:
        existing = [
            candidate
            for candidate in _artifact_paths(reservation.paths.temp_path)
            if os.path.lexists(candidate)
        ]
        if not existing:
            return
        details: dict[str, Any] = {
            "operation_id": reservation.operation_id,
            "snapshot_id": reservation.snapshot_id,
            "temp_evidence": [candidate.name for candidate in existing],
        }
        sidecars = [candidate for candidate in existing if candidate != reservation.paths.temp_path]
        if not os.path.lexists(reservation.paths.temp_path) or sidecars:
            self._mark_recovery_required(
                reservation.operation_id,
                code="DUPLICATE_TEMP_UNSAFE",
                details={**details, "reason": "missing_temp_or_sidecar_evidence"},
            )
            raise LifecycleOperationError(
                "Operation-owned temporary publication evidence is ambiguous.",
                code="OPERATION_RECOVERY_REQUIRED",
                details=details,
            )
        try:
            temp_artifact = self._verify_artifact(
                reservation.paths.temp_path,
                reservation,
                descriptor=descriptor,
                compute_hash=True,
            )
            self._require_witness_match(witness, temp_artifact, reservation)
        except LifecycleOperationError as verify_exc:
            self._mark_recovery_required(
                reservation.operation_id,
                code="DUPLICATE_TEMP_UNSAFE",
                details={**details, "cause": verify_exc.to_dict()},
            )
            raise LifecycleOperationError(
                "Duplicate temporary artifact is unsafe or mismatched.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={**details, "cause": verify_exc.to_dict()},
            ) from verify_exc
        if (
            temp_artifact.file_size != final_artifact.file_size
            or temp_artifact.file_sha256 != final_artifact.file_sha256
        ):
            self._mark_recovery_required(
                reservation.operation_id,
                code="DUPLICATE_TEMP_UNSAFE",
                details={**details, "reason": "whole_file_identity_mismatch"},
            )
            raise LifecycleOperationError(
                "Duplicate temporary artifact does not match the published file.",
                code="OPERATION_RECOVERY_REQUIRED",
                details=details,
            )
        try:
            reservation.paths.temp_path.unlink()
        except OSError as cleanup_exc:
            failure_details = {
                **details,
                "error_type": type(cleanup_exc).__name__,
                "winerror": getattr(cleanup_exc, "winerror", None),
            }
            if not _is_windows_sharing_violation(cleanup_exc):
                self._mark_recovery_required(
                    reservation.operation_id,
                    code="DUPLICATE_TEMP_CLEANUP_FAILED",
                    details=failure_details,
                )
                raise LifecycleOperationError(
                    "Matching duplicate temp cleanup failed closed.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details=failure_details,
                ) from cleanup_exc
            self._mark_recovery_required(
                reservation.operation_id,
                code="DUPLICATE_TEMP_CLEANUP_BLOCKED",
                details=failure_details,
            )
            raise LifecycleOperationError(
                "Matching collision candidate could not release the duplicate temp artifact.",
                code="PUBLICATION_SHARING_VIOLATION",
                retryable=True,
                details={
                    "operation_id": reservation.operation_id,
                    "winerror": getattr(cleanup_exc, "winerror", None),
                },
            ) from cleanup_exc
        remaining = [
            candidate
            for candidate in _artifact_paths(reservation.paths.temp_path)
            if os.path.lexists(candidate)
        ]
        if remaining:
            incomplete_details = {
                **details,
                "remaining_evidence": [candidate.name for candidate in remaining],
            }
            self._mark_recovery_required(
                reservation.operation_id,
                code="DUPLICATE_TEMP_CLEANUP_INCOMPLETE",
                details=incomplete_details,
            )
            raise LifecycleOperationError(
                "Duplicate temp cleanup did not prove complete absence.",
                code="OPERATION_RECOVERY_REQUIRED",
                details=incomplete_details,
            )

    def _require_operation_temp_absent(self, reservation: OperationReservation) -> None:
        existing = [
            candidate
            for candidate in _artifact_paths(reservation.paths.temp_path)
            if os.path.lexists(candidate)
        ]
        if not existing:
            return
        details = {
            "operation_id": reservation.operation_id,
            "snapshot_id": reservation.snapshot_id,
            "temp_evidence": [candidate.name for candidate in existing],
        }
        self._mark_recovery_required(
            reservation.operation_id,
            code="DUPLICATE_TEMP_EVIDENCE_REMAINS",
            details=details,
        )
        raise LifecycleOperationError(
            "Operation-owned temporary evidence remains before finalization.",
            code="OPERATION_RECOVERY_REQUIRED",
            details=details,
        )

    def _record_sealed_witness(
        self,
        reservation: OperationReservation,
        verified: _VerifiedArtifact,
    ) -> dict[str, Any]:
        witness = {
            "version": 1,
            "operation_id": reservation.operation_id,
            "snapshot_id": reservation.snapshot_id,
            "relative_temp_path": reservation.paths.relative_temp_path,
            "file_size": verified.file_size,
            "file_sha256": verified.file_sha256,
            "run_id": verified.metadata["run_id"],
            "logical_fingerprint": verified.metadata["logical_fingerprint"],
            "observation_fingerprint": verified.metadata["observation_fingerprint"],
        }
        conflict = False
        with self._registry() as conn, immediate_registry_transaction(conn):
            operation = self._require_operation(conn, reservation.operation_id)
            reservation = self._require_reservation_identity(conn, operation, reservation)
            self._require_active_lease(operation, reservation)
            if operation["operation_phase"] not in {
                "BUILDING",
                "FILE_PUBLISHED",
                "RECOVERY_REQUIRED",
            }:
                raise LifecycleOperationError(
                    "Operation cannot record a sealed artifact from its current phase.",
                    code="OPERATION_PHASE_CONFLICT",
                    details={"operation_phase": operation["operation_phase"]},
                )
            self._require_current_binding(conn, reservation)
            self._require_task_finalization_preconditions(
                conn,
                reservation,
                finalizing=False,
            )
            self._require_pointer_preconditions(
                conn,
                _request_from_reservation(reservation),
                task_id=reservation.task_id,
                finalizing=False,
            )
            existing = self._sealed_witness_from_conn(conn, reservation)
            if existing is None:
                self._append_event(
                    conn,
                    project_id=reservation.project_id,
                    event_type=_SEALED_EVENT_TYPE,
                    message="Operation-owned lifecycle artifact sealed before publication.",
                    details=witness,
                )
            elif existing != witness:
                failure = _canonical_json(
                    {
                        "code": "SEALED_WITNESS_CONFLICT",
                        "details": {"operation_id": reservation.operation_id},
                    }
                )
                self._update_operation_phase(
                    conn,
                    operation,
                    phase="RECOVERY_REQUIRED",
                    failure_json=failure,
                )
                conflict = True
        if conflict:
            raise LifecycleOperationError(
                "Operation already owns a different durable seal witness.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={"operation_id": reservation.operation_id},
            )
        return witness

    def _sealed_witness(self, reservation: OperationReservation) -> dict[str, Any] | None:
        with self._registry() as conn:
            return self._sealed_witness_from_conn(conn, reservation)

    def _sealed_witness_from_conn(
        self,
        conn: sqlite3.Connection,
        reservation: OperationReservation,
    ) -> dict[str, Any] | None:
        matches: list[dict[str, Any]] = []
        rows = conn.execute(
            """SELECT details_json FROM registry_events
               WHERE project_id = ? AND event_type = ?
               ORDER BY created_at, event_id""",
            (reservation.project_id, _SEALED_EVENT_TYPE),
        ).fetchall()
        for row in rows:
            details = _json_object(row["details_json"], field="sealed_witness")
            if details.get("operation_id") == reservation.operation_id:
                matches.append(details)
        if not matches:
            return None
        expected = matches[0]
        if any(candidate != expected for candidate in matches[1:]):
            raise LifecycleOperationError(
                "Operation has conflicting durable seal witnesses.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={"operation_id": reservation.operation_id},
            )
        required = {
            "version": 1,
            "operation_id": reservation.operation_id,
            "snapshot_id": reservation.snapshot_id,
            "relative_temp_path": reservation.paths.relative_temp_path,
        }
        if any(expected.get(key) != value for key, value in required.items()):
            raise LifecycleOperationError(
                "Operation seal witness does not match its durable reservation.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={"operation_id": reservation.operation_id},
            )
        if (
            not isinstance(expected.get("file_size"), int)
            or expected["file_size"] < 0
            or not _is_sha256(expected.get("file_sha256"))
            or not all(
                isinstance(expected.get(field), str) and expected[field]
                for field in ("run_id", "logical_fingerprint", "observation_fingerprint")
            )
        ):
            raise LifecycleOperationError(
                "Operation seal witness is malformed.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={"operation_id": reservation.operation_id},
            )
        return expected

    def _require_witness_match(
        self,
        witness: Mapping[str, Any],
        verified: _VerifiedArtifact,
        reservation: OperationReservation,
    ) -> None:
        actual = {
            "file_size": verified.file_size,
            "file_sha256": verified.file_sha256,
            "run_id": verified.metadata["run_id"],
            "logical_fingerprint": verified.metadata["logical_fingerprint"],
            "observation_fingerprint": verified.metadata["observation_fingerprint"],
        }
        mismatches = {
            key: {"expected": witness.get(key), "actual": value}
            for key, value in actual.items()
            if witness.get(key) != value
        }
        if mismatches:
            raise LifecycleOperationError(
                "Published artifact does not match the operation-local seal witness.",
                code="GENERATION_ARTIFACT_MISMATCH",
                details={
                    "operation_id": reservation.operation_id,
                    "mismatches": mismatches,
                },
            )

    def _verify_artifact(
        self,
        path: Path,
        reservation: OperationReservation,
        *,
        descriptor: SealedArtifactDescriptor | None,
        compute_hash: bool,
    ) -> _VerifiedArtifact:
        try:
            require_managed_regular_file(path, generation_root=reservation.paths.generation_root)
        except RegistryOperationError as exc:
            raise LifecycleOperationError(
                "Lifecycle artifact is not a managed regular file.",
                code="GENERATION_ARTIFACT_INVALID",
                details={"path": str(path), "cause": exc.to_dict()},
            ) from exc
        sidecars = [
            candidate for candidate in _artifact_paths(path)[1:] if os.path.lexists(candidate)
        ]
        if sidecars:
            raise LifecycleOperationError(
                "Lifecycle artifact requires SQLite sidecar files.",
                code="GENERATION_ARTIFACT_INVALID",
                details={"sidecars": [str(candidate) for candidate in sidecars]},
            )
        try:
            metadata = validate_snapshot(
                path,
                project_id=reservation.project_id,
                expected_repo_root_norm=reservation.repository_root_norm,
                expected_repository_identity_hash=reservation.repository_identity_hash,
                expected_repository_binding_generation=reservation.workspace_binding_generation,
                require_v2=True,
            )
        except SnapshotQueryError as exc:
            raise LifecycleOperationError(
                "Lifecycle artifact failed read-only semantic validation.",
                code="GENERATION_ARTIFACT_INVALID",
                details={"path": str(path), "cause": exc.to_dict()},
            ) from exc
        before = _json_object(metadata["repo_state_before_json"], field="repo_state_before_json")
        after = _json_object(metadata["repo_state_after_json"], field="repo_state_after_json")
        mismatches: dict[str, Any] = {}
        expected = {
            "snapshot_id": reservation.snapshot_id,
            "project_id": reservation.project_id,
            "repo_root_norm": reservation.repository_root_norm,
            "repository_identity_hash": reservation.repository_identity_hash,
            "repository_binding_generation": reservation.workspace_binding_generation,
            "git_head": reservation.expected_head,
            "git_branch": reservation.expected_branch,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "build_status": "SEALED",
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                mismatches[key] = {"expected": value, "actual": metadata.get(key)}
        actual_contract = _contract_fingerprint_from_metadata(metadata)
        if actual_contract != reservation.capture_contract_fingerprint:
            mismatches["capture_contract_fingerprint"] = {
                "expected": reservation.capture_contract_fingerprint,
                "actual": actual_contract,
            }
        if before != after:
            mismatches["repository_state"] = "before_after_mismatch"
        file_size = path.stat().st_size
        if descriptor is not None:
            descriptor_expected = {
                "snapshot_id": descriptor.snapshot_id,
                "run_id": descriptor.run_id,
                "logical_fingerprint": descriptor.logical_fingerprint,
                "observation_fingerprint": descriptor.observation_fingerprint,
            }
            for key, value in descriptor_expected.items():
                if metadata.get(key) != value:
                    mismatches[f"descriptor_{key}"] = {
                        "expected": value,
                        "actual": metadata.get(key),
                    }
            descriptor_workspace = descriptor.workspace
            if (
                descriptor_workspace.project_id != reservation.project_id
                or descriptor_workspace.workspace_id != reservation.workspace_id
                or descriptor_workspace.repo_root_norm != reservation.repository_root_norm
                or descriptor_workspace.repository_identity_hash
                != reservation.repository_identity_hash
                or descriptor_workspace.binding_generation
                != reservation.workspace_binding_generation
            ):
                mismatches["descriptor_workspace"] = "mismatch"
            if descriptor.size_bytes != file_size:
                mismatches["descriptor_size_bytes"] = {
                    "expected": descriptor.size_bytes,
                    "actual": file_size,
                }
            descriptor_before = json.loads(
                _canonical_json(descriptor.repository_state_before.to_dict())
            )
            descriptor_after = json.loads(
                _canonical_json(descriptor.repository_state_after.to_dict())
            )
            descriptor_final = json.loads(
                _canonical_json(descriptor.repository_state_final.to_dict())
            )
            if descriptor_before != before:
                mismatches["descriptor_repository_state_before"] = "mismatch"
            if descriptor_after != after:
                mismatches["descriptor_repository_state_after"] = "mismatch"
            if descriptor_final != after:
                mismatches["descriptor_repository_state_final"] = "mismatch"
        if mismatches:
            raise LifecycleOperationError(
                "Lifecycle artifact identity does not match its durable reservation.",
                code="GENERATION_ARTIFACT_MISMATCH",
                details={"mismatches": mismatches},
            )
        repository_evidence = {
            "snapshot_id": metadata["snapshot_id"],
            "run_id": metadata["run_id"],
            "repo_root_norm": metadata["repo_root_norm"],
            "repository_identity_hash": metadata["repository_identity_hash"],
            "repository_binding_generation": metadata["repository_binding_generation"],
            "git_head": metadata["git_head"],
            "git_branch": metadata["git_branch"],
            "git_status_fingerprint": metadata["git_status_fingerprint"],
            "repo_state_before": before,
            "repo_state_after": after,
            "repo_state_final": after,
            "logical_fingerprint": metadata["logical_fingerprint"],
            "observation_fingerprint": metadata["observation_fingerprint"],
        }
        return _VerifiedArtifact(
            metadata=metadata,
            file_size=file_size,
            file_sha256=_hash_file(path) if compute_hash else "",
            repository_evidence_json=_canonical_json(repository_evidence),
        )

    def _require_prepublication_state(
        self,
        reservation: OperationReservation,
    ) -> OperationReservation:
        try:
            request = _request_from_reservation(reservation)
            lineage_witness = self._preflight_lineage(
                request,
                expected_sequence=reservation.generation_sequence,
                expected_predecessor=reservation.predecessor_pointer,
            )
            with self._registry() as conn:
                row = self._require_operation(conn, reservation.operation_id)
                reservation = self._require_reservation_identity(conn, row, reservation)
                self._require_active_lease(row, reservation)
                if row["operation_phase"] not in {
                    "BUILDING",
                    "FILE_PUBLISHED",
                    "RECOVERY_REQUIRED",
                }:
                    raise LifecycleOperationError(
                        "Operation is not ready for generation publication.",
                        code="OPERATION_PHASE_CONFLICT",
                        details={
                            "operation_id": reservation.operation_id,
                            "operation_phase": row["operation_phase"],
                        },
                    )
                self._require_current_binding(conn, reservation)
                self._require_task_finalization_preconditions(
                    conn,
                    reservation,
                    finalizing=False,
                )
                self._require_pointer_preconditions(
                    conn,
                    request,
                    task_id=reservation.task_id,
                    finalizing=False,
                )
                self._require_generation_shape(
                    conn,
                    request,
                    reservation.generation_sequence,
                    lineage_witness=lineage_witness,
                    expected_predecessor=reservation.predecessor_pointer,
                )
                return reservation
        except LifecycleOperationError as exc:
            if exc.code not in _TERMINAL_PREPUBLICATION_CODES:
                raise
            raise self._terminalize_prepublication_conflict(reservation, exc) from exc

    def _terminalize_prepublication_conflict(
        self,
        reservation: OperationReservation,
        conflict: LifecycleOperationError,
    ) -> LifecycleOperationError:
        details = {
            "operation_id": reservation.operation_id,
            "snapshot_id": reservation.snapshot_id,
            "cause": conflict.to_dict(),
        }
        if os.path.lexists(reservation.paths.final_path):
            self._mark_recovery_required(
                reservation.operation_id,
                code=conflict.code,
                details=details,
            )
            return LifecycleOperationError(
                "Conflict occurred after final-file evidence became ambiguous.",
                code="OPERATION_RECOVERY_REQUIRED",
                details=details,
            )
        if self._cleanup_operation_temp(reservation.paths):
            self._mark_failed(
                reservation.operation_id,
                code=conflict.code,
                details=details,
            )
            return conflict
        self._mark_recovery_required(
            reservation.operation_id,
            code="TEMP_CLEANUP_AMBIGUOUS",
            details=details,
        )
        return LifecycleOperationError(
            "Pre-publication conflict could not safely release operation-owned evidence.",
            code="OPERATION_RECOVERY_REQUIRED",
            details=details,
        )

    def _ensure_file_published_phase(self, reservation: OperationReservation) -> None:
        with self._registry() as conn, immediate_registry_transaction(conn):
            row = self._require_operation(conn, reservation.operation_id)
            reservation = self._require_reservation_identity(conn, row, reservation)
            self._require_active_lease(row, reservation)
            phase = row["operation_phase"]
            if phase == "RESERVED":
                self._update_operation_phase(conn, row, phase="BUILDING")
                row = self._require_operation(conn, reservation.operation_id)
                phase = "BUILDING"
            if phase == "BUILDING":
                self._update_operation_phase(conn, row, phase="FILE_PUBLISHED")
                self._append_event(
                    conn,
                    project_id=reservation.project_id,
                    event_type="lifecycle_generation_file_published",
                    message="Immutable lifecycle generation file published.",
                    details={
                        "operation_id": reservation.operation_id,
                        "snapshot_id": reservation.snapshot_id,
                        "relative_storage_path": reservation.paths.relative_final_path,
                    },
                )
            elif phase not in {"FILE_PUBLISHED", "RECOVERY_REQUIRED"}:
                raise LifecycleOperationError(
                    "Operation cannot record file publication from its current phase.",
                    code="OPERATION_PHASE_CONFLICT",
                    details={
                        "operation_id": reservation.operation_id,
                        "operation_phase": phase,
                    },
                )

    def _finalize_or_orphan(
        self,
        reservation: OperationReservation,
        verified: _VerifiedArtifact,
    ) -> GenerationPublicationResult:
        self._require_operation_temp_absent(reservation)
        try:
            try:
                lineage_witness = self._preflight_lineage(
                    _request_from_reservation(reservation),
                    expected_sequence=reservation.generation_sequence,
                    expected_predecessor=reservation.predecessor_pointer,
                )
            except LifecycleOperationError as exc:
                raise _FinalizationConflict(
                    exc.code,
                    "Generation predecessor became unusable after file publication.",
                    {"cause": exc.to_dict()},
                ) from exc
            return self._finalize_publication(
                reservation,
                verified,
                lineage_witness=lineage_witness,
            )
        except _FinalizationConflict as conflict:
            with self._registry() as conn:
                operation = self._require_operation(conn, reservation.operation_id)
                reservation = self._require_reservation_identity(
                    conn,
                    operation,
                    reservation,
                )
                already_committed = operation["operation_phase"] == "COMMITTED"
            if already_committed:
                return self._committed_result(reservation, replayed=True)
            self._register_orphan(reservation, verified, conflict)
            raise LifecycleOperationError(
                conflict.message,
                code=conflict.code,
                details={
                    **conflict.details,
                    "operation_id": reservation.operation_id,
                    "snapshot_id": reservation.snapshot_id,
                    "generation_state": "ORPHANED",
                },
            ) from conflict

    def _finalize_publication(
        self,
        reservation: OperationReservation,
        verified: _VerifiedArtifact,
        *,
        lineage_witness: _LineageWitness | None,
    ) -> GenerationPublicationResult:
        with self._registry() as conn:
            try:
                with immediate_registry_transaction(conn):
                    operation = self._require_operation(conn, reservation.operation_id)
                    reservation = self._require_reservation_identity(
                        conn,
                        operation,
                        reservation,
                    )
                    self._require_active_lease(operation, reservation)
                    if operation["operation_phase"] not in {
                        "FILE_PUBLISHED",
                        "RECOVERY_REQUIRED",
                    }:
                        raise _FinalizationConflict(
                            "OPERATION_PHASE_CONFLICT",
                            "Operation is not in a registrable publication phase.",
                            {"operation_phase": operation["operation_phase"]},
                        )
                    self._require_current_binding(conn, reservation, finalizing=True)
                    self._require_task_finalization_preconditions(
                        conn,
                        reservation,
                        finalizing=True,
                    )
                    request = _request_from_reservation(reservation)
                    self._require_pointer_preconditions(
                        conn,
                        request,
                        task_id=reservation.task_id,
                        finalizing=True,
                    )
                    try:
                        self._require_generation_shape(
                            conn,
                            request,
                            reservation.generation_sequence,
                            lineage_witness=lineage_witness,
                            expected_predecessor=reservation.predecessor_pointer,
                        )
                    except LifecycleOperationError as exc:
                        raise _FinalizationConflict(
                            exc.code,
                            "Generation predecessor changed before registry commit.",
                            {"cause": exc.to_dict()},
                        ) from exc
                    existing_generation = conn.execute(
                        "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                        (reservation.snapshot_id,),
                    ).fetchone()
                    if existing_generation is not None:
                        raise _FinalizationConflict(
                            "GENERATION_REGISTRATION_CONFLICT",
                            "Reserved generation identity is already registered.",
                            {"generation_state": existing_generation["generation_state"]},
                        )
                    timestamp = self._timestamp()
                    conn.execute(
                        """INSERT INTO snapshot_generations (
                               snapshot_id, project_id, workspace_id,
                               workspace_binding_generation, task_id, generation_sequence,
                               parent_snapshot_id, capture_purpose, origin_operation_id,
                               capture_contract_fingerprint, repository_evidence_json,
                               relative_storage_path, storage_layout_version, file_size,
                               file_sha256, truth_claim, generation_state, row_version,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                     'CAPTURED_STABLE', 'AVAILABLE', 0, ?, ?)""",
                        (
                            reservation.snapshot_id,
                            reservation.project_id,
                            reservation.workspace_id,
                            reservation.workspace_binding_generation,
                            reservation.task_id,
                            reservation.generation_sequence,
                            reservation.parent_snapshot_id,
                            reservation.capture_purpose,
                            reservation.operation_id,
                            reservation.capture_contract_fingerprint,
                            verified.repository_evidence_json,
                            reservation.paths.relative_final_path,
                            GENERATION_STORAGE_LAYOUT_VERSION,
                            verified.file_size,
                            verified.file_sha256,
                            timestamp,
                            timestamp,
                        ),
                    )
                    pointer_versions = self._apply_pointer_plan(conn, reservation, timestamp)
                    task_state, task_row_version = self._finalize_task_for_generation(
                        conn,
                        reservation,
                        timestamp,
                    )
                    current_operation = self._require_operation(conn, reservation.operation_id)
                    self._update_operation_phase(
                        conn,
                        current_operation,
                        phase="COMMITTED",
                        failure_json=None,
                    )
                    self._append_event(
                        conn,
                        project_id=reservation.project_id,
                        event_type="lifecycle_generation_committed",
                        message="Lifecycle generation registered and task pointer committed.",
                        details={
                            "operation_id": reservation.operation_id,
                            "snapshot_id": reservation.snapshot_id,
                            "generation_sequence": reservation.generation_sequence,
                            "pointer_versions": dict(pointer_versions),
                            "task_state": task_state,
                            "task_row_version": task_row_version,
                        },
                    )
                return GenerationPublicationResult(
                    operation_id=reservation.operation_id,
                    snapshot_id=reservation.snapshot_id,
                    generation_sequence=reservation.generation_sequence,
                    generation_state="AVAILABLE",
                    relative_storage_path=reservation.paths.relative_final_path,
                    file_size=verified.file_size,
                    file_sha256=verified.file_sha256,
                    pointer_versions=pointer_versions,
                    task_state=task_state,
                    task_row_version=task_row_version,
                    replayed=False,
                )
            except sqlite3.IntegrityError as exc:
                raise _FinalizationConflict(
                    "REGISTRY_CAS_CONFLICT",
                    "Generation registration or pointer CAS lost a concurrent race.",
                    {"sqlite_error": str(exc)},
                ) from exc

    def _finalize_task_for_generation(
        self,
        conn: sqlite3.Connection,
        reservation: OperationReservation,
        timestamp: str,
    ) -> tuple[str, int]:
        if reservation.activate_task_on_commit:
            cursor = conn.execute(
                """UPDATE lifecycle_tasks
                   SET task_state = 'ACTIVE', row_version = row_version + 1, updated_at = ?
                   WHERE task_id = ? AND project_id = ? AND workspace_id = ?
                     AND workspace_binding_generation = ? AND task_state = 'DRAFT'
                     AND row_version = ?
                     AND next_generation_sequence = ?""",
                (
                    timestamp,
                    reservation.task_id,
                    reservation.project_id,
                    reservation.workspace_id,
                    reservation.workspace_binding_generation,
                    reservation.reserved_task_version,
                    reservation.generation_sequence + 1,
                ),
            )
            if cursor.rowcount != 1:
                raise _FinalizationConflict(
                    "TASK_VERSION_CONFLICT",
                    "DRAFT task could not transition to ACTIVE with its baseline commit.",
                    {
                        "task_id": reservation.task_id,
                        "expected_task_version": reservation.reserved_task_version,
                    },
                )
        task = conn.execute(
            "SELECT task_state, row_version FROM lifecycle_tasks WHERE task_id = ?",
            (reservation.task_id,),
        ).fetchone()
        expected_state = (
            "ACTIVE"
            if reservation.activate_task_on_commit
            else _OPERATION_SPECS[reservation.operation_kind][2]
        )
        expected_version = reservation.reserved_task_version + (
            1 if reservation.activate_task_on_commit else 0
        )
        if (
            task is None
            or task["task_state"] != expected_state
            or task["row_version"] != expected_version
        ):
            raise _FinalizationConflict(
                "TASK_VERSION_CONFLICT",
                "Task result evidence changed before operation commit.",
                {
                    "task_id": reservation.task_id,
                    "expected_task_state": expected_state,
                    "expected_task_version": expected_version,
                },
            )
        return task["task_state"], task["row_version"]

    def _register_orphan(
        self,
        reservation: OperationReservation,
        verified: _VerifiedArtifact,
        conflict: _FinalizationConflict,
    ) -> None:
        with self._registry() as conn, immediate_registry_transaction(conn):
            operation = self._require_operation(conn, reservation.operation_id)
            existing = conn.execute(
                "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                (reservation.snapshot_id,),
            ).fetchone()
            timestamp = self._timestamp()
            if existing is None:
                conn.execute(
                    """INSERT INTO snapshot_generations (
                               snapshot_id, project_id, workspace_id,
                               workspace_binding_generation, task_id, generation_sequence,
                               parent_snapshot_id, capture_purpose, origin_operation_id,
                               capture_contract_fingerprint, repository_evidence_json,
                               relative_storage_path, storage_layout_version, file_size,
                               file_sha256, truth_claim, generation_state, row_version,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                     'CAPTURED_STABLE', 'ORPHANED', 0, ?, ?)""",
                    (
                        reservation.snapshot_id,
                        reservation.project_id,
                        reservation.workspace_id,
                        reservation.workspace_binding_generation,
                        reservation.task_id,
                        reservation.generation_sequence,
                        reservation.parent_snapshot_id,
                        reservation.capture_purpose,
                        reservation.operation_id,
                        reservation.capture_contract_fingerprint,
                        verified.repository_evidence_json,
                        reservation.paths.relative_final_path,
                        GENERATION_STORAGE_LAYOUT_VERSION,
                        verified.file_size,
                        verified.file_sha256,
                        timestamp,
                        timestamp,
                    ),
                )
            failure_json = _canonical_json({"code": conflict.code, "details": conflict.details})
            phase = operation["operation_phase"]
            if phase not in _TERMINAL_PHASES and phase != "RECOVERY_REQUIRED":
                self._update_operation_phase(
                    conn,
                    operation,
                    phase="RECOVERY_REQUIRED",
                    failure_json=failure_json,
                )
                operation = self._require_operation(conn, reservation.operation_id)
                phase = "RECOVERY_REQUIRED"
            if phase == "RECOVERY_REQUIRED":
                self._update_operation_phase(
                    conn,
                    operation,
                    phase="FAILED",
                    failure_json=failure_json,
                )
            self._append_event(
                conn,
                project_id=reservation.project_id,
                event_type="lifecycle_generation_orphaned",
                message="Published generation lost its registry CAS and was orphaned.",
                details={
                    "operation_id": reservation.operation_id,
                    "snapshot_id": reservation.snapshot_id,
                    "conflict_code": conflict.code,
                },
            )

    def _apply_pointer_plan(
        self,
        conn: sqlite3.Connection,
        reservation: OperationReservation,
        timestamp: str,
    ) -> tuple[tuple[str, int], ...]:
        versions: list[tuple[str, int]] = []
        for expectation in reservation.pointer_expectations:
            if expectation.expected_pointer_version is None:
                conn.execute(
                    """INSERT INTO managed_pointers (
                           pointer_id, project_id, task_id, pointer_role, snapshot_id,
                           pointer_version, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        expectation.pointer_id,
                        reservation.project_id,
                        reservation.task_id,
                        expectation.pointer_role,
                        reservation.snapshot_id,
                        timestamp,
                        timestamp,
                    ),
                )
                versions.append((expectation.pointer_role, 0))
            else:
                version = _compare_and_swap_pointer(
                    conn,
                    expectation.pointer_id,
                    expected_pointer_version=expectation.expected_pointer_version,
                    snapshot_id=reservation.snapshot_id,
                    updated_at=timestamp,
                )
                versions.append((expectation.pointer_role, version))
        return tuple(versions)

    def _require_pointer_preconditions(
        self,
        conn: sqlite3.Connection,
        request: GenerationReservationRequest,
        *,
        task_id: str,
        finalizing: bool,
    ) -> None:
        for expectation in request.pointer_expectations:
            row = conn.execute(
                """SELECT * FROM managed_pointers
                   WHERE project_id = ? AND task_id = ? AND pointer_role = ?""",
                (request.project_id, task_id, expectation.pointer_role),
            ).fetchone()
            details = {
                "pointer_id": expectation.pointer_id,
                "pointer_role": expectation.pointer_role,
                "expected_pointer_version": expectation.expected_pointer_version,
                "expected_snapshot_id": expectation.expected_snapshot_id,
            }
            if expectation.expected_pointer_version is None:
                valid = row is None and expectation.expected_snapshot_id is None
            else:
                valid = (
                    row is not None
                    and row["pointer_id"] == expectation.pointer_id
                    and row["pointer_version"] == expectation.expected_pointer_version
                    and row["snapshot_id"] == expectation.expected_snapshot_id
                )
            if not valid:
                if finalizing:
                    raise _FinalizationConflict(
                        "POINTER_CAS_CONFLICT",
                        "Managed pointer compare-and-swap precondition failed.",
                        details,
                    )
                raise LifecycleOperationError(
                    "Managed pointer expectation is stale or ambiguous.",
                    code="POINTER_CAS_CONFLICT",
                    details=details,
                )

    def _require_generation_shape(
        self,
        conn: sqlite3.Connection,
        request: GenerationReservationRequest,
        sequence: int,
        *,
        lineage_witness: _LineageWitness | None,
        expected_predecessor: PointerExpectation | None = None,
    ) -> None:
        capture_purpose = _OPERATION_SPECS[request.operation_kind][0]
        if capture_purpose == "TASK_BASELINE":
            if (
                sequence != 0
                or request.parent_snapshot_id is not None
                or lineage_witness is not None
                or expected_predecessor is not None
            ):
                raise LifecycleOperationError(
                    "Baseline generation must be sequence zero without a predecessor.",
                    code="GENERATION_LINEAGE_INVALID",
                    details={
                        "capture_purpose": capture_purpose,
                        "generation_sequence": sequence,
                        "parent_snapshot_id": request.parent_snapshot_id,
                    },
                )
            return
        if sequence <= 0 or request.parent_snapshot_id is None or lineage_witness is None:
            raise LifecycleOperationError(
                "Reserved sequence/parent does not match the capture purpose.",
                code="GENERATION_LINEAGE_INVALID",
                details={
                    "capture_purpose": capture_purpose,
                    "generation_sequence": sequence,
                    "parent_snapshot_id": request.parent_snapshot_id,
                },
            )
        pointer, parent = self._lineage_rows(
            conn,
            request,
            sequence=sequence,
            expected_predecessor=expected_predecessor,
        )
        expected_witness = {
            "snapshot_id": parent["snapshot_id"],
            "generation_sequence": parent["generation_sequence"],
            "capture_purpose": parent["capture_purpose"],
            "origin_operation_id": parent["origin_operation_id"],
            "generation_row_version": parent["row_version"],
            "file_size": parent["file_size"],
            "file_sha256": parent["file_sha256"],
            "pointer_id": pointer["pointer_id"],
            "pointer_role": pointer["pointer_role"],
            "pointer_version": pointer["pointer_version"],
        }
        mismatches = {
            key: {"expected": value, "actual": getattr(lineage_witness, key)}
            for key, value in expected_witness.items()
            if getattr(lineage_witness, key) != value
        }
        if mismatches:
            raise LifecycleOperationError(
                "Authoritative predecessor changed after immutable-file validation.",
                code="POINTER_CAS_CONFLICT",
                details={
                    "parent_snapshot_id": request.parent_snapshot_id,
                    "mismatches": sorted(mismatches),
                },
            )

    def _preflight_lineage(
        self,
        request: GenerationReservationRequest,
        *,
        expected_sequence: int | None = None,
        expected_predecessor: PointerExpectation | None = None,
    ) -> _LineageWitness | None:
        if request.operation_kind == "BEGIN_TASK":
            return None
        with self._registry() as conn:
            task = conn.execute(
                "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
                (request.task_id,),
            ).fetchone()
            expected_state = _OPERATION_SPECS[request.operation_kind][2]
            if (
                task is None
                or task["project_id"] != request.project_id
                or task["workspace_id"] != request.workspace_id
                or task["workspace_binding_generation"] != request.workspace_binding_generation
                or task["task_state"] != expected_state
            ):
                raise LifecycleOperationError(
                    "Lifecycle task cannot own the requested generation predecessor.",
                    code="TASK_OWNERSHIP_MISMATCH",
                    details={"task_id": request.task_id},
                )
            if expected_sequence is None:
                if task["row_version"] != request.expected_task_version:
                    raise LifecycleOperationError(
                        "Lifecycle task row version is stale.",
                        code="TASK_VERSION_CONFLICT",
                        details={
                            "task_id": request.task_id,
                            "expected_task_version": request.expected_task_version,
                            "actual_task_version": task["row_version"],
                        },
                    )
                sequence = task["next_generation_sequence"]
            else:
                sequence = expected_sequence
            pointer, parent = self._lineage_rows(
                conn,
                request,
                sequence=sequence,
                expected_predecessor=expected_predecessor,
            )
            parent_row = dict(parent)
            pointer_row = dict(pointer)

        descriptor = self._resolve_predecessor(parent_row["snapshot_id"])
        descriptor_mismatches = {
            field: {"registry": parent_row[field], "resolved": value}
            for field, value in {
                "snapshot_id": descriptor.snapshot_id,
                "project_id": descriptor.project_id,
                "workspace_id": descriptor.workspace_id,
                "workspace_binding_generation": descriptor.workspace_binding_generation,
                "task_id": descriptor.task_id,
                "generation_sequence": descriptor.generation_sequence,
                "capture_purpose": descriptor.capture_purpose,
                "origin_operation_id": descriptor.origin_operation_id,
                "file_size": descriptor.file_size,
                "file_sha256": descriptor.file_sha256,
            }.items()
            if parent_row[field] != value
        }
        if descriptor_mismatches:
            raise LifecycleOperationError(
                "Resolved predecessor disagrees with its immutable registry row.",
                code="GENERATION_PARENT_INVALID",
                details={
                    "parent_snapshot_id": parent_row["snapshot_id"],
                    "mismatches": sorted(descriptor_mismatches),
                },
            )
        return _LineageWitness(
            snapshot_id=parent_row["snapshot_id"],
            generation_sequence=parent_row["generation_sequence"],
            capture_purpose=parent_row["capture_purpose"],
            origin_operation_id=parent_row["origin_operation_id"],
            generation_row_version=parent_row["row_version"],
            file_size=descriptor.file_size,
            file_sha256=descriptor.file_sha256,
            pointer_id=pointer_row["pointer_id"],
            pointer_role=pointer_row["pointer_role"],
            pointer_version=pointer_row["pointer_version"],
        )

    def _lineage_rows(
        self,
        conn: sqlite3.Connection,
        request: GenerationReservationRequest,
        *,
        sequence: int,
        expected_predecessor: PointerExpectation | None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        publication_pointer = request.pointer_expectations[0]
        if request.operation_kind == "REFRESH_WORKING":
            if publication_pointer.expected_pointer_version is None:
                predecessor_role = "TASK_BASELINE"
                expected_parent_purpose = "TASK_BASELINE"
            else:
                predecessor_role = "TASK_LATEST_WORKING"
                expected_parent_purpose = "TASK_WORKING"
        elif request.operation_kind == "ACCEPT_TASK":
            predecessor_role = "TASK_LATEST_WORKING"
            expected_parent_purpose = "TASK_WORKING"
        else:
            raise LifecycleOperationError(
                "Operation does not permit a generation predecessor.",
                code="GENERATION_LINEAGE_INVALID",
                details={"operation_kind": request.operation_kind},
            )
        pointer = conn.execute(
            """SELECT * FROM managed_pointers
               WHERE project_id = ? AND task_id = ? AND pointer_role = ?""",
            (request.project_id, request.task_id, predecessor_role),
        ).fetchone()
        if pointer is None:
            raise LifecycleOperationError(
                "Authoritative task predecessor pointer is missing.",
                code="GENERATION_LINEAGE_INVALID",
                details={"pointer_role": predecessor_role},
            )
        if (
            predecessor_role == "TASK_LATEST_WORKING"
            and request.operation_kind == "REFRESH_WORKING"
            and (
                pointer["pointer_id"] != publication_pointer.pointer_id
                or pointer["pointer_version"] != publication_pointer.expected_pointer_version
                or pointer["snapshot_id"] != publication_pointer.expected_snapshot_id
            )
        ):
            raise LifecycleOperationError(
                "Latest-working predecessor no longer matches the pinned CAS expectation.",
                code="POINTER_CAS_CONFLICT",
                details={
                    "pointer_id": publication_pointer.pointer_id,
                    "pointer_role": predecessor_role,
                    "expected_pointer_version": publication_pointer.expected_pointer_version,
                    "expected_snapshot_id": publication_pointer.expected_snapshot_id,
                },
            )
        if expected_predecessor is not None and (
            pointer["pointer_id"] != expected_predecessor.pointer_id
            or pointer["pointer_role"] != expected_predecessor.pointer_role
            or pointer["pointer_version"] != expected_predecessor.expected_pointer_version
            or pointer["snapshot_id"] != expected_predecessor.expected_snapshot_id
        ):
            raise LifecycleOperationError(
                "Authoritative predecessor pointer changed after reservation.",
                code="POINTER_CAS_CONFLICT",
                details={
                    "pointer_id": expected_predecessor.pointer_id,
                    "pointer_role": expected_predecessor.pointer_role,
                    "expected_pointer_version": expected_predecessor.expected_pointer_version,
                    "expected_snapshot_id": expected_predecessor.expected_snapshot_id,
                },
            )
        if request.parent_snapshot_id != pointer["snapshot_id"]:
            raise LifecycleOperationError(
                "Generation parent is not the authoritative task-pointer predecessor.",
                code="GENERATION_LINEAGE_INVALID",
                details={
                    "parent_snapshot_id": request.parent_snapshot_id,
                    "authoritative_snapshot_id": pointer["snapshot_id"],
                    "pointer_role": predecessor_role,
                },
            )
        parent = conn.execute(
            "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
            (request.parent_snapshot_id,),
        ).fetchone()
        if (
            parent is None
            or parent["project_id"] != request.project_id
            or parent["workspace_id"] != request.workspace_id
            or parent["workspace_binding_generation"] != request.workspace_binding_generation
            or parent["task_id"] != request.task_id
            or parent["generation_state"] != "AVAILABLE"
            or parent["capture_purpose"] != expected_parent_purpose
        ):
            raise LifecycleOperationError(
                "Authoritative predecessor is unavailable or belongs to another lineage.",
                code="GENERATION_PARENT_INVALID",
                details={"parent_snapshot_id": request.parent_snapshot_id},
            )
        if parent["generation_sequence"] >= sequence:
            raise LifecycleOperationError(
                "Generation parent must have a lower reserved sequence.",
                code="GENERATION_LINEAGE_INVALID",
                details={
                    "parent_snapshot_id": request.parent_snapshot_id,
                    "parent_sequence": parent["generation_sequence"],
                    "generation_sequence": sequence,
                },
            )
        return pointer, parent

    def _resolve_predecessor(self, snapshot_id: str) -> GenerationDescriptor:
        try:
            return self.resolve_generation(snapshot_id)
        except LifecycleOperationError as exc:
            if exc.code not in {"GENERATION_MISSING", "GENERATION_CORRUPT"}:
                raise
            self._raise_broken_generation(snapshot_id, exc)
            raise AssertionError("broken generation classification must raise") from exc

    def _raise_broken_generation(
        self,
        snapshot_id: str,
        cause: LifecycleOperationError,
    ) -> None:
        quarantined = False
        pointers: list[dict[str, Any]] = []
        with self._registry() as conn, immediate_registry_transaction(conn):
            generation = conn.execute(
                "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if generation is None:
                raise cause
            rows = conn.execute(
                """SELECT pointer.pointer_id, pointer.pointer_role,
                          pointer.pointer_version, pointer.task_id, task.task_state
                   FROM managed_pointers AS pointer
                   LEFT JOIN lifecycle_tasks AS task ON task.task_id = pointer.task_id
                   WHERE pointer.snapshot_id = ?""",
                (snapshot_id,),
            ).fetchall()
            pointers = [dict(row) for row in rows]
            live = [
                pointer
                for pointer in pointers
                if pointer["pointer_role"] == "PROJECT_BASELINE"
                or pointer["task_state"] != "CLOSED"
            ]
            if live:
                raise LifecycleOperationError(
                    "Live pointer references a missing or corrupt immutable generation.",
                    code="BROKEN_POINTER",
                    details={
                        "snapshot_id": snapshot_id,
                        "pointers": live,
                        "cause": cause.to_dict(),
                    },
                ) from cause
            if generation["generation_state"] == "AVAILABLE":
                cursor = conn.execute(
                    """UPDATE snapshot_generations
                       SET generation_state = 'QUARANTINED', row_version = row_version + 1,
                           updated_at = ?
                       WHERE snapshot_id = ? AND row_version = ?
                         AND generation_state = 'AVAILABLE'""",
                    (self._timestamp(), snapshot_id, generation["row_version"]),
                )
                quarantined = cursor.rowcount == 1
                if quarantined:
                    self._append_event(
                        conn,
                        project_id=generation["project_id"],
                        event_type="lifecycle_generation_quarantined",
                        message="Broken unpointed immutable generation quarantined.",
                        details={
                            "snapshot_id": snapshot_id,
                            "cause": cause.to_dict(),
                        },
                    )
        raise LifecycleOperationError(
            "Immutable generation is missing or corrupt and cannot seed lineage.",
            code="GENERATION_BROKEN",
            details={
                "snapshot_id": snapshot_id,
                "generation_state": "QUARANTINED" if quarantined else None,
                "pointers": pointers,
                "cause": cause.to_dict(),
            },
        ) from cause

    def _insert_begin_task(
        self,
        conn: sqlite3.Connection,
        request: GenerationReservationRequest,
        begin_task: BeginTaskReservation,
    ) -> None:
        if (
            request.expected_task_version != 0
            or request.parent_snapshot_id is not None
            or not _is_sha256(begin_task.capture_contract_fingerprint)
            or begin_task.baseline_head_commit != request.expected_head
            or begin_task.baseline_head_ref != request.expected_branch
        ):
            raise LifecycleOperationError(
                "BEGIN_TASK reservation facts are inconsistent.",
                code="LIFECYCLE_REQUEST_INVALID",
            )
        if (begin_task.project_baseline_snapshot_id is None) != (
            begin_task.project_baseline_pointer_version is None
        ):
            raise LifecycleOperationError(
                "Project-baseline snapshot and version must be pinned together.",
                code="LIFECYCLE_REQUEST_INVALID",
            )
        if begin_task.project_baseline_snapshot_id is not None:
            _require_id(
                begin_task.project_baseline_snapshot_id,
                field="project_baseline_snapshot_id",
            )
            if (
                not isinstance(begin_task.project_baseline_pointer_version, int)
                or isinstance(begin_task.project_baseline_pointer_version, bool)
                or begin_task.project_baseline_pointer_version < 0
            ):
                raise LifecycleOperationError(
                    "Project-baseline pointer version is invalid.",
                    code="LIFECYCLE_REQUEST_INVALID",
                )
        existing_task = conn.execute(
            "SELECT task_id FROM lifecycle_tasks WHERE task_id = ?",
            (request.task_id,),
        ).fetchone()
        if existing_task is not None:
            raise LifecycleOperationError(
                "Generated task identity already exists.",
                code="TASK_RESERVATION_CONFLICT",
                details={"task_id": request.task_id},
            )
        open_task = conn.execute(
            """SELECT task_id, task_state FROM lifecycle_tasks
               WHERE workspace_id = ? AND workspace_binding_generation = ?
                 AND task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
               LIMIT 1""",
            (request.workspace_id, request.workspace_binding_generation),
        ).fetchone()
        if open_task is not None:
            raise LifecycleOperationError(
                "Workspace binding already owns an open task lineage.",
                code="OPEN_TASK_EXISTS",
                details={
                    "task_id": open_task["task_id"],
                    "task_state": open_task["task_state"],
                },
            )
        baseline = conn.execute(
            """SELECT snapshot_id, pointer_version FROM managed_pointers
               WHERE project_id = ? AND task_id IS NULL
                 AND pointer_role = 'PROJECT_BASELINE'""",
            (request.project_id,),
        ).fetchone()
        actual_baseline = (
            None
            if baseline is None
            else (
                baseline["snapshot_id"],
                baseline["pointer_version"],
            )
        )
        expected_baseline = (
            None
            if begin_task.project_baseline_snapshot_id is None
            else (
                begin_task.project_baseline_snapshot_id,
                begin_task.project_baseline_pointer_version,
            )
        )
        if actual_baseline != expected_baseline:
            raise LifecycleOperationError(
                "Project baseline changed before task reservation.",
                code="PROJECT_BASELINE_CHANGED",
                details={"expected": expected_baseline, "actual": actual_baseline},
            )
        timestamp = self._timestamp()
        try:
            conn.execute(
                """INSERT INTO lifecycle_tasks (
                       task_id, project_id, workspace_id, workspace_binding_generation,
                       task_state, capture_contract_fingerprint, baseline_head_commit,
                       baseline_head_ref, next_generation_sequence,
                       project_baseline_snapshot_id, project_baseline_pointer_version,
                       row_version, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, 0, ?, ?, 0, ?, ?)""",
                (
                    request.task_id,
                    request.project_id,
                    request.workspace_id,
                    request.workspace_binding_generation,
                    begin_task.capture_contract_fingerprint,
                    begin_task.baseline_head_commit,
                    begin_task.baseline_head_ref,
                    begin_task.project_baseline_snapshot_id,
                    begin_task.project_baseline_pointer_version,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LifecycleOperationError(
                "Task/open-lineage reservation lost a concurrent registry race.",
                code="TASK_RESERVATION_CONFLICT",
                details={"sqlite_error": str(exc)},
            ) from exc

    def _require_task_for_reservation(
        self,
        conn: sqlite3.Connection,
        request: GenerationReservationRequest,
    ) -> sqlite3.Row:
        task = conn.execute(
            "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
            (request.task_id,),
        ).fetchone()
        expected_state = _OPERATION_SPECS[request.operation_kind][2]
        if (
            task is None
            or task["project_id"] != request.project_id
            or task["workspace_id"] != request.workspace_id
            or task["workspace_binding_generation"] != request.workspace_binding_generation
        ):
            raise LifecycleOperationError(
                "Lifecycle task ownership does not match the reservation request.",
                code="TASK_OWNERSHIP_MISMATCH",
                details={"task_id": request.task_id},
            )
        if task["task_state"] != expected_state:
            raise LifecycleOperationError(
                "Lifecycle task state does not permit this generation operation.",
                code="TASK_STATE_CONFLICT",
                details={
                    "task_id": request.task_id,
                    "expected_state": expected_state,
                    "actual_state": task["task_state"],
                },
            )
        if task["row_version"] != request.expected_task_version:
            raise LifecycleOperationError(
                "Lifecycle task row version is stale.",
                code="TASK_VERSION_CONFLICT",
                details={
                    "task_id": request.task_id,
                    "expected_task_version": request.expected_task_version,
                    "actual_task_version": task["row_version"],
                },
            )
        if not isinstance(task["capture_contract_fingerprint"], str):
            raise LifecycleOperationError(
                "Lifecycle task capture contract is invalid.",
                code="TASK_CAPTURE_CONTRACT_INVALID",
                details={"task_id": request.task_id},
            )
        return task

    def _require_task_finalization_preconditions(
        self,
        conn: sqlite3.Connection,
        reservation: OperationReservation,
        *,
        finalizing: bool,
    ) -> None:
        task = conn.execute(
            "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
            (reservation.task_id,),
        ).fetchone()
        expected_state = _OPERATION_SPECS[reservation.operation_kind][2]
        valid = (
            task is not None
            and task["project_id"] == reservation.project_id
            and task["workspace_id"] == reservation.workspace_id
            and task["workspace_binding_generation"] == reservation.workspace_binding_generation
            and task["task_state"] == expected_state
            and task["row_version"] == reservation.reserved_task_version
            and task["next_generation_sequence"] == reservation.generation_sequence + 1
            and task["capture_contract_fingerprint"] == reservation.capture_contract_fingerprint
        )
        if valid:
            return
        details = {
            "task_id": reservation.task_id,
            "expected_task_version": reservation.reserved_task_version,
            "actual_task_version": task["row_version"] if task is not None else None,
        }
        if finalizing:
            raise _FinalizationConflict(
                "TASK_VERSION_CONFLICT",
                "Task preconditions changed after file publication.",
                details,
            )
        raise LifecycleOperationError(
            "Task preconditions changed before file publication.",
            code="TASK_VERSION_CONFLICT",
            details=details,
        )

    def _require_workspace(
        self,
        conn: sqlite3.Connection,
        request: GenerationReservationRequest,
    ) -> sqlite3.Row:
        workspace = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?",
            (request.workspace_id,),
        ).fetchone()
        if (
            workspace is None
            or workspace["project_id"] != request.project_id
            or workspace["workspace_binding_generation"] != request.workspace_binding_generation
            or workspace["workspace_state"] != "ACTIVE"
            or workspace["repository_fingerprint_json"] is None
        ):
            raise LifecycleOperationError(
                "Workspace ownership/binding is unavailable or mismatched.",
                code="WORKSPACE_BINDING_MISMATCH",
                details={"workspace_id": request.workspace_id},
            )
        return workspace

    def _require_current_binding(
        self,
        conn: sqlite3.Connection,
        reservation: OperationReservation,
        *,
        finalizing: bool = False,
    ) -> None:
        workspace = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?",
            (reservation.workspace_id,),
        ).fetchone()
        actual_identity = None
        if workspace is not None and workspace["repository_fingerprint_json"] is not None:
            actual_identity = repository_identity_hash(
                workspace["workspace_root_norm"],
                workspace["repository_fingerprint_json"],
            )
        valid = (
            workspace is not None
            and workspace["project_id"] == reservation.project_id
            and workspace["workspace_binding_generation"]
            == reservation.workspace_binding_generation
            and workspace["workspace_state"] == "ACTIVE"
            and workspace["workspace_root_norm"] == reservation.repository_root_norm
            and actual_identity == reservation.repository_identity_hash
        )
        if valid:
            return
        details = {"workspace_id": reservation.workspace_id}
        if finalizing:
            raise _FinalizationConflict(
                "WORKSPACE_BINDING_CONFLICT",
                "Workspace binding changed after file publication.",
                details,
            )
        raise LifecycleOperationError(
            "Workspace binding changed before file publication.",
            code="WORKSPACE_BINDING_CONFLICT",
            details=details,
        )

    def _reservation_from_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        replayed: bool = False,
        lease_reclaimed: bool = False,
    ) -> OperationReservation:
        del conn
        durable_plan = _json_object(
            row["expected_pointer_versions_json"],
            field="operation_plan",
        )
        normalized_request = _persisted_request_plan(durable_plan)
        if row["request_fingerprint"] != _fingerprint(normalized_request):
            raise LifecycleOperationError(
                "Persisted request fingerprint does not match its normalized plan.",
                code="OPERATION_IDENTITY_MISMATCH",
                details={"operation_id": row["operation_id"]},
            )
        pointers = tuple(
            _pointer_from_dict(value) for value in normalized_request.get("pointers", ())
        )
        predecessor_value = durable_plan.get("predecessor_pointer")
        predecessor_pointer = (
            None if predecessor_value is None else _pointer_from_dict(predecessor_value)
        )
        expected_row = {
            "operation_kind": normalized_request.get("operation_kind"),
            "project_id": normalized_request.get("project_id"),
            "workspace_id": normalized_request.get("workspace_id"),
            "workspace_binding_generation": normalized_request.get("workspace_binding_generation"),
            "task_id": normalized_request.get("task_id"),
            "expected_task_version": normalized_request.get("expected_task_version"),
        }
        row_mismatches = {
            field: {"row": row[field], "plan": value}
            for field, value in expected_row.items()
            if row[field] != value
        }
        actor_context = _canonical_json(normalized_request.get("actor_context"))
        if row["actor_context_json"] != actor_context:
            row_mismatches["actor_context_json"] = "mismatch"
        capture_purpose = durable_plan.get("capture_purpose")
        capture_contract = durable_plan.get("capture_contract_fingerprint")
        repository_root_norm = durable_plan.get("repository_root_norm")
        repository_identity = durable_plan.get("repository_identity_hash")
        activate_task_on_commit = normalized_request.get("activate_task_on_commit", False)
        if (
            capture_purpose != normalized_request.get("capture_purpose")
            or not isinstance(capture_contract, str)
            or not _is_sha256(capture_contract)
            or not isinstance(repository_root_norm, str)
            or not repository_root_norm
            or not isinstance(repository_identity, str)
            or not _is_sha256(repository_identity)
            or not isinstance(activate_task_on_commit, bool)
            or (
                activate_task_on_commit and normalized_request.get("operation_kind") != "BEGIN_TASK"
            )
        ):
            row_mismatches["durable_operation_plan"] = "invalid"
        if row_mismatches:
            raise LifecycleOperationError(
                "Persisted operation row disagrees with its immutable request plan.",
                code="OPERATION_IDENTITY_MISMATCH",
                details={
                    "operation_id": row["operation_id"],
                    "mismatches": row_mismatches,
                },
            )
        paths = managed_generation_paths(
            self.home,
            snapshot_id=row["reserved_snapshot_id"],
            operation_id=row["operation_id"],
        )
        _require_generation_storage_isolated(
            paths,
            repository_root=repository_root_norm,
        )
        if (
            row["managed_temp_path"] != paths.relative_temp_path
            or row["managed_final_path"] != paths.relative_final_path
        ):
            raise LifecycleOperationError(
                "Operation managed paths do not match the canonical layout.",
                code="OPERATION_IDENTITY_MISMATCH",
                details={"operation_id": row["operation_id"]},
            )
        owner, token = _lease_parts(row["lease_owner"])
        return OperationReservation(
            operation_id=row["operation_id"],
            request_fingerprint=row["request_fingerprint"],
            operation_kind=row["operation_kind"],
            operation_phase=row["operation_phase"],
            project_id=row["project_id"],
            workspace_id=row["workspace_id"],
            workspace_binding_generation=row["workspace_binding_generation"],
            task_id=row["task_id"],
            snapshot_id=row["reserved_snapshot_id"],
            generation_sequence=row["reserved_generation_sequence"],
            capture_purpose=capture_purpose,
            capture_contract_fingerprint=capture_contract,
            repository_root_norm=repository_root_norm,
            repository_identity_hash=repository_identity,
            parent_snapshot_id=normalized_request.get("parent_snapshot_id"),
            pointer_expectations=pointers,
            predecessor_pointer=predecessor_pointer,
            expected_task_version=row["expected_task_version"],
            reserved_task_version=row["expected_task_version"] + 1,
            expected_head=normalized_request.get("expected_head"),
            expected_branch=normalized_request.get("expected_branch"),
            activate_task_on_commit=activate_task_on_commit,
            paths=paths,
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=row["lease_expires_at"],
            replayed=replayed,
            lease_reclaimed=lease_reclaimed,
        )

    def _require_operation(self, conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise LifecycleOperationError(
                "Lifecycle operation is not registered.",
                code="OPERATION_NOT_FOUND",
                details={"operation_id": operation_id},
            )
        return row

    def _require_reservation_identity(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        reservation: OperationReservation,
        *,
        require_phase: bool = False,
    ) -> OperationReservation:
        canonical = self._reservation_from_row(conn, row)
        excluded = {"replayed", "lease_reclaimed"}
        if not require_phase:
            excluded.add("operation_phase")
        mismatches = {
            field.name: {
                "persisted": getattr(canonical, field.name),
                "caller": getattr(reservation, field.name),
            }
            for field in fields(OperationReservation)
            if field.name not in excluded
            and getattr(canonical, field.name) != getattr(reservation, field.name)
        }
        if mismatches:
            raise LifecycleOperationError(
                "Caller reservation does not equal the complete durable reservation.",
                code="OPERATION_IDENTITY_MISMATCH",
                details={
                    "operation_id": reservation.operation_id,
                    "mismatches": sorted(mismatches),
                },
            )
        return canonical

    def _canonical_caller_reservation(
        self,
        reservation: OperationReservation,
    ) -> OperationReservation:
        with self._registry() as conn:
            row = self._require_operation(conn, reservation.operation_id)
            canonical = self._require_reservation_identity(
                conn,
                row,
                reservation,
                require_phase=True,
            )
            self._require_active_lease(row, canonical)
            return canonical

    def _load_reservation(self, operation_id: str) -> OperationReservation:
        with self._registry() as conn:
            row = self._require_operation(conn, operation_id)
            return self._reservation_from_row(conn, row)

    def _require_active_lease(
        self,
        row: sqlite3.Row,
        reservation: OperationReservation,
    ) -> None:
        owner, token = _lease_parts(row["lease_owner"])
        if (
            owner != reservation.lease_owner
            or token != reservation.lease_token
            or _parse_timestamp(row["lease_expires_at"]) <= self._now()
        ):
            raise LifecycleOperationError(
                "Operation lease is stale, expired, or owned by another caller.",
                code="OPERATION_LEASE_CONFLICT",
                retryable=True,
                details={
                    "operation_id": reservation.operation_id,
                    "lease_expires_at": row["lease_expires_at"],
                },
            )

    def _update_operation_phase(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        phase: str,
        failure_json: str | None | object = ...,
    ) -> None:
        if not conn.in_transaction:
            raise RegistryOperationError("Operation transition requires an owned transaction.")
        failure_value = row["failure_json"] if failure_json is ... else failure_json
        cursor = conn.execute(
            """UPDATE lifecycle_operations
               SET operation_phase = ?, failure_json = ?,
                   row_version = row_version + 1, updated_at = ?
               WHERE operation_id = ? AND row_version = ?""",
            (
                phase,
                failure_value,
                self._timestamp(),
                row["operation_id"],
                row["row_version"],
            ),
        )
        if cursor.rowcount != 1:
            raise LifecycleOperationError(
                "Operation phase compare-and-swap failed.",
                code="OPERATION_PHASE_CONFLICT",
                retryable=True,
                details={"operation_id": row["operation_id"]},
            )

    def _mark_recovery_required(
        self,
        operation_id: str,
        *,
        code: str,
        details: dict[str, Any],
    ) -> None:
        with self._registry() as conn, immediate_registry_transaction(conn):
            row = self._require_operation(conn, operation_id)
            if row["operation_phase"] in _TERMINAL_PHASES:
                return
            failure = _canonical_json({"code": code, "details": details})
            self._update_operation_phase(
                conn,
                row,
                phase="RECOVERY_REQUIRED",
                failure_json=failure,
            )

    def _mark_failed(
        self,
        operation_id: str,
        *,
        code: str,
        details: dict[str, Any],
    ) -> None:
        with self._registry() as conn, immediate_registry_transaction(conn):
            row = self._require_operation(conn, operation_id)
            if row["operation_phase"] in _TERMINAL_PHASES:
                return
            failure = _canonical_json({"code": code, "details": details})
            if row["operation_phase"] == "FILE_PUBLISHED":
                self._update_operation_phase(
                    conn,
                    row,
                    phase="RECOVERY_REQUIRED",
                    failure_json=failure,
                )
                row = self._require_operation(conn, operation_id)
            self._update_operation_phase(
                conn,
                row,
                phase="FAILED",
                failure_json=failure,
            )

    def _cleanup_operation_temp(self, paths: ManagedGenerationPaths) -> bool:
        existing = [
            candidate
            for candidate in _artifact_paths(paths.temp_path)
            if os.path.lexists(candidate)
        ]
        try:
            for candidate in existing:
                require_managed_regular_file(
                    candidate,
                    generation_root=paths.generation_root,
                )
        except RegistryOperationError:
            return False
        for candidate in reversed(existing):
            try:
                candidate.unlink()
            except OSError:
                return False
        return True

    def _committed_result(
        self,
        reservation: OperationReservation,
        *,
        replayed: bool,
    ) -> GenerationPublicationResult:
        self._require_operation_temp_absent(reservation)
        descriptor = self._resolve_predecessor(reservation.snapshot_id)
        if descriptor.origin_operation_id != reservation.operation_id:
            raise LifecycleOperationError(
                "Committed generation origin does not match the durable operation.",
                code="OPERATION_RECOVERY_REQUIRED",
                details={
                    "operation_id": reservation.operation_id,
                    "snapshot_id": reservation.snapshot_id,
                },
            )
        with self._registry() as conn:
            operation = self._require_operation(conn, reservation.operation_id)
            reservation = self._reservation_from_row(conn, operation)
            if operation["operation_phase"] != "COMMITTED":
                raise LifecycleOperationError(
                    "Generation exists without a committed durable operation result.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={"operation_id": reservation.operation_id},
                )
            committed_events: list[dict[str, Any]] = []
            rows = conn.execute(
                """SELECT details_json FROM registry_events
                   WHERE project_id = ? AND event_type = 'lifecycle_generation_committed'
                   ORDER BY created_at, event_id""",
                (reservation.project_id,),
            ).fetchall()
            for row in rows:
                details = _json_object(row["details_json"], field="committed_event")
                if details.get("operation_id") == reservation.operation_id:
                    committed_events.append(details)
            expected_versions = tuple(
                (
                    expectation.pointer_role,
                    0
                    if expectation.expected_pointer_version is None
                    else expectation.expected_pointer_version + 1,
                )
                for expectation in reservation.pointer_expectations
            )
            expected_version_map = dict(expected_versions)
            expected_task_state = (
                "ACTIVE"
                if reservation.activate_task_on_commit
                else _OPERATION_SPECS[reservation.operation_kind][2]
            )
            expected_task_version = reservation.reserved_task_version + (
                1 if reservation.activate_task_on_commit else 0
            )
            committed_event = committed_events[0] if len(committed_events) == 1 else {}
            event_task_state = committed_event.get("task_state")
            event_task_version = committed_event.get("task_row_version")
            task_result_evidence_valid = (
                event_task_state == expected_task_state
                and event_task_version == expected_task_version
            ) or (
                not reservation.activate_task_on_commit
                and event_task_state is None
                and event_task_version is None
            )
            valid_event = (
                len(committed_events) == 1
                and committed_event.get("snapshot_id") == reservation.snapshot_id
                and committed_event.get("generation_sequence") == reservation.generation_sequence
                and committed_event.get("pointer_versions") == expected_version_map
                and task_result_evidence_valid
            )
            if not valid_event:
                raise LifecycleOperationError(
                    "Committed operation result evidence is missing or inconsistent.",
                    code="OPERATION_RECOVERY_REQUIRED",
                    details={
                        "operation_id": reservation.operation_id,
                        "snapshot_id": reservation.snapshot_id,
                    },
                )
            for expectation in reservation.pointer_expectations:
                if expectation.pointer_role == "TASK_LATEST_WORKING":
                    continue
                pointer = conn.execute(
                    "SELECT * FROM managed_pointers WHERE pointer_id = ?",
                    (expectation.pointer_id,),
                ).fetchone()
                if (
                    pointer is None
                    or pointer["snapshot_id"] != reservation.snapshot_id
                    or pointer["pointer_role"] != expectation.pointer_role
                    or pointer["pointer_version"] != expected_version_map[expectation.pointer_role]
                ):
                    raise LifecycleOperationError(
                        "Committed immutable pointer evidence is missing or inconsistent.",
                        code="BROKEN_POINTER",
                        details={
                            "operation_id": reservation.operation_id,
                            "pointer_id": expectation.pointer_id,
                        },
                    )
        return GenerationPublicationResult(
            operation_id=reservation.operation_id,
            snapshot_id=reservation.snapshot_id,
            generation_sequence=reservation.generation_sequence,
            generation_state=descriptor.generation_state,
            relative_storage_path=descriptor.relative_storage_path,
            file_size=descriptor.file_size,
            file_sha256=descriptor.file_sha256,
            pointer_versions=expected_versions,
            task_state=expected_task_state,
            task_row_version=expected_task_version,
            replayed=replayed,
        )

    def _failed_operation_error(self, operation_id: str) -> LifecycleOperationError:
        with self._registry() as conn:
            row = self._require_operation(conn, operation_id)
            failure = row["failure_json"]
        return LifecycleOperationError(
            "The exact lifecycle operation is terminally failed.",
            code="OPERATION_FAILED",
            details={"operation_id": operation_id, "failure": failure},
        )

    def _generation_row(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._registry() as conn:
            row = conn.execute(
                "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        event_type: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        project = conn.execute(
            "SELECT project_name FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO registry_events (
                   event_id, project_id, project_name, event_type,
                   message, details_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                self._new_id(),
                project_id,
                project["project_name"] if project is not None else None,
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
        ) as conn:
            if conn is None:
                raise LifecycleOperationError(
                    "Lifecycle operation requires an existing registry.",
                    code="REGISTRY_NOT_FOUND",
                )
            yield conn

    def _new_id(self) -> str:
        value = self._id_factory()
        _require_id(value, field="generated_id")
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Lifecycle clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _timestamp(self) -> str:
        return _iso_timestamp(self._now())

    def _checkpoint(self, name: str) -> None:
        if self._checkpoint_callback is not None:
            self._checkpoint_callback(name)


def _validate_reservation_request(request: GenerationReservationRequest) -> None:
    for field, value in (
        ("project_id", request.project_id),
        ("workspace_id", request.workspace_id),
        ("workspace_binding_generation", request.workspace_binding_generation),
        ("task_id", request.task_id),
    ):
        _require_id(value, field=field)
    if request.operation_kind not in _OPERATION_SPECS:
        raise LifecycleOperationError(
            "Operation kind is not generation-producing or is outside Slice 7B2.",
            code="OPERATION_KIND_NOT_SUPPORTED",
            details={"operation_kind": request.operation_kind},
        )
    if (
        not isinstance(request.idempotency_key, str)
        or not request.idempotency_key
        or len(request.idempotency_key) > 256
    ):
        raise LifecycleOperationError(
            "Idempotency key must contain 1-256 characters.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "idempotency_key"},
        )
    if (
        not isinstance(request.expected_task_version, int)
        or isinstance(request.expected_task_version, bool)
        or request.expected_task_version < 0
    ):
        raise LifecycleOperationError(
            "Expected task version is invalid.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "expected_task_version"},
        )
    if request.parent_snapshot_id is not None:
        _require_id(request.parent_snapshot_id, field="parent_snapshot_id")
    if len(request.pointer_expectations) != 1:
        raise LifecycleOperationError(
            "Slice 7B2 requires exactly one task-pointer plan.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "pointer_expectations"},
        )
    expected_role = _OPERATION_SPECS[request.operation_kind][1]
    for pointer in request.pointer_expectations:
        _require_id(pointer.pointer_id, field="pointer_id")
        if pointer.pointer_role != expected_role or pointer.pointer_role == "PROJECT_BASELINE":
            raise LifecycleOperationError(
                "Pointer role is not permitted for this internal generation operation.",
                code="POINTER_ROLE_NOT_SUPPORTED",
                details={
                    "pointer_role": pointer.pointer_role,
                    "expected_pointer_role": expected_role,
                },
            )
        if pointer.expected_pointer_version is None:
            if pointer.expected_snapshot_id is not None:
                raise LifecycleOperationError(
                    "Pointer creation cannot name a predecessor snapshot.",
                    code="LIFECYCLE_REQUEST_INVALID",
                )
        else:
            if (
                not isinstance(pointer.expected_pointer_version, int)
                or isinstance(pointer.expected_pointer_version, bool)
                or pointer.expected_pointer_version < 0
                or pointer.expected_snapshot_id is None
            ):
                raise LifecycleOperationError(
                    "Pointer CAS expectation is invalid.",
                    code="LIFECYCLE_REQUEST_INVALID",
                )
            _require_id(pointer.expected_snapshot_id, field="expected_snapshot_id")
    if not isinstance(request.actor_context, Mapping) or not request.actor_context:
        raise LifecycleOperationError(
            "Actor/invocation context must be supplied by the caller boundary.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "actor_context"},
        )
    _canonical_json(dict(request.actor_context))
    if not isinstance(request.activate_task_on_commit, bool) or (
        request.activate_task_on_commit and request.operation_kind != "BEGIN_TASK"
    ):
        raise LifecycleOperationError(
            "Task activation is permitted only for BEGIN_TASK.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "activate_task_on_commit"},
        )


def _validate_lease(lease: OperationLease) -> None:
    if not isinstance(lease.owner, str) or not lease.owner or len(lease.owner) > 256:
        raise LifecycleOperationError(
            "Lease owner is invalid.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "lease_owner"},
        )
    if not isinstance(lease.token, str) or not lease.token or len(lease.token) > 256:
        raise LifecycleOperationError(
            "Lease token is invalid.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "lease_token"},
        )
    if (
        not isinstance(lease.duration_seconds, int)
        or isinstance(lease.duration_seconds, bool)
        or not 1 <= lease.duration_seconds <= 86_400
    ):
        raise LifecycleOperationError(
            "Lease duration must be between 1 and 86400 seconds.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": "lease_duration_seconds"},
        )


def _request_plan(request: GenerationReservationRequest) -> dict[str, Any]:
    plan = {
        "version": 1,
        "operation_kind": request.operation_kind,
        "project_id": request.project_id,
        "workspace_id": request.workspace_id,
        "workspace_binding_generation": request.workspace_binding_generation,
        "task_id": request.task_id,
        "expected_task_version": request.expected_task_version,
        "capture_purpose": _OPERATION_SPECS[request.operation_kind][0],
        "parent_snapshot_id": request.parent_snapshot_id,
        "pointers": [_pointer_dict(pointer) for pointer in request.pointer_expectations],
        "expected_head": request.expected_head,
        "expected_branch": request.expected_branch,
        "actor_context": dict(request.actor_context),
    }
    if request.activate_task_on_commit:
        plan["activate_task_on_commit"] = True
    return plan


def _persisted_operation_plan(
    normalized_request: Mapping[str, Any],
    *,
    capture_contract_fingerprint: str,
    repository_root_norm: str,
    repository_identity_hash_value: str,
    predecessor_pointer: PointerExpectation | None,
) -> dict[str, Any]:
    return {
        "version": 2,
        "normalized_request": dict(normalized_request),
        "capture_purpose": normalized_request["capture_purpose"],
        "capture_contract_fingerprint": capture_contract_fingerprint,
        "repository_root_norm": repository_root_norm,
        "repository_identity_hash": repository_identity_hash_value,
        "predecessor_pointer": (
            None if predecessor_pointer is None else _pointer_dict(predecessor_pointer)
        ),
    }


def _persisted_request_plan(durable_plan: Mapping[str, Any]) -> dict[str, Any]:
    if durable_plan.get("version") != 2:
        raise LifecycleOperationError(
            "Persisted operation plan version is unsupported or incomplete.",
            code="OPERATION_IDENTITY_MISMATCH",
        )
    normalized = durable_plan.get("normalized_request")
    if not isinstance(normalized, dict) or normalized.get("version") != 1:
        raise LifecycleOperationError(
            "Persisted normalized lifecycle request is invalid.",
            code="OPERATION_IDENTITY_MISMATCH",
        )
    return normalized


def _request_from_reservation(
    reservation: OperationReservation,
) -> GenerationReservationRequest:
    return GenerationReservationRequest(
        idempotency_key="recovered-from-reservation",
        operation_kind=reservation.operation_kind,
        project_id=reservation.project_id,
        workspace_id=reservation.workspace_id,
        workspace_binding_generation=reservation.workspace_binding_generation,
        task_id=reservation.task_id,
        expected_task_version=reservation.expected_task_version,
        parent_snapshot_id=reservation.parent_snapshot_id,
        pointer_expectations=reservation.pointer_expectations,
        expected_head=reservation.expected_head,
        expected_branch=reservation.expected_branch,
        actor_context={"recovered": True},
        activate_task_on_commit=reservation.activate_task_on_commit,
    )


def _request_from_normalized_plan(
    idempotency_key: str,
    normalized: Mapping[str, Any],
) -> GenerationReservationRequest:
    pointers = normalized.get("pointers")
    if not isinstance(pointers, list):
        raise LifecycleOperationError(
            "Persisted lifecycle pointer plan is invalid.",
            code="OPERATION_IDENTITY_MISMATCH",
        )
    request = GenerationReservationRequest(
        idempotency_key=idempotency_key,
        operation_kind=normalized.get("operation_kind"),
        project_id=normalized.get("project_id"),
        workspace_id=normalized.get("workspace_id"),
        workspace_binding_generation=normalized.get("workspace_binding_generation"),
        task_id=normalized.get("task_id"),
        expected_task_version=normalized.get("expected_task_version"),
        parent_snapshot_id=normalized.get("parent_snapshot_id"),
        pointer_expectations=tuple(_pointer_from_dict(value) for value in pointers),
        expected_head=normalized.get("expected_head"),
        expected_branch=normalized.get("expected_branch"),
        actor_context=normalized.get("actor_context"),
        activate_task_on_commit=normalized.get("activate_task_on_commit", False),
    )
    _validate_reservation_request(request)
    return request


def _pointer_dict(pointer: PointerExpectation) -> dict[str, Any]:
    return {
        "pointer_id": pointer.pointer_id,
        "pointer_role": pointer.pointer_role,
        "expected_pointer_version": pointer.expected_pointer_version,
        "expected_snapshot_id": pointer.expected_snapshot_id,
    }


def _pointer_from_dict(value: object) -> PointerExpectation:
    if not isinstance(value, dict):
        raise LifecycleOperationError(
            "Persisted pointer expectation is invalid.",
            code="OPERATION_RECOVERY_REQUIRED",
        )
    return PointerExpectation(
        pointer_id=value.get("pointer_id"),
        pointer_role=value.get("pointer_role"),
        expected_pointer_version=value.get("expected_pointer_version"),
        expected_snapshot_id=value.get("expected_snapshot_id"),
    )


def _contract_fingerprint_from_metadata(metadata: Mapping[str, Any]) -> str:
    policy = _json_object(metadata["policy_json"], field="policy_json")
    extractors = metadata["extractor_versions"]
    payload = {
        "fingerprint_version": _CAPTURE_CONTRACT_FINGERPRINT_VERSION,
        "schema_version": metadata["schema_version"],
        "scanner_version": metadata["scanner_version"],
        "policy": policy,
        "extractor_versions": sorted(extractors.items()),
        "proof_contract_version": metadata["proof_contract_version"],
        "verifier_version": metadata["verifier_version"],
        "module_map_version": metadata["module_map_version"],
        "occurrence_contract_version": metadata["occurrence_contract_version"],
        "generation_storage_layout_version": GENERATION_STORAGE_LAYOUT_VERSION,
        "capture_scope_exclusion_contract": {
            "policy_version": policy["policy_version"],
            "discovered_metadata_paths": policy["discovered_metadata_paths"],
            "discovered_pruned_roots": policy["discovered_pruned_roots"],
        },
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _repository_root_from_evidence(value: str) -> str:
    return str(_json_object(value, field="repository_evidence_json")["repo_root_norm"])


def _repository_identity_from_evidence(value: str) -> str:
    return str(_json_object(value, field="repository_evidence_json")["repository_identity_hash"])


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_paths(path: Path) -> tuple[Path, ...]:
    return (path, *(Path(f"{path}{suffix}") for suffix in _SIDECAR_SUFFIXES))


def _is_windows_sharing_violation(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise LifecycleOperationError(
            "Lifecycle request/evidence is not canonical JSON.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"error_type": type(exc).__name__},
        ) from exc


def _json_object(value: object, *, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise LifecycleOperationError(
            "Persisted lifecycle JSON evidence is invalid.",
            code="OPERATION_RECOVERY_REQUIRED",
            details={"field": field},
        ) from exc
    if not isinstance(parsed, dict):
        raise LifecycleOperationError(
            "Persisted lifecycle JSON evidence must be an object.",
            code="OPERATION_RECOVERY_REQUIRED",
            details={"field": field},
        )
    return parsed


def _lease_json(lease: OperationLease) -> str:
    return _canonical_json({"owner": lease.owner, "token": lease.token})


def _lease_parts(value: object) -> tuple[str, str]:
    parsed = _json_object(value, field="lease_owner")
    owner = parsed.get("owner")
    token = parsed.get("token")
    if not isinstance(owner, str) or not isinstance(token, str):
        raise LifecycleOperationError(
            "Persisted operation lease evidence is invalid.",
            code="OPERATION_RECOVERY_REQUIRED",
        )
    return owner, token


def _require_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise LifecycleOperationError(
            "Lifecycle identity is invalid.",
            code="LIFECYCLE_REQUEST_INVALID",
            details={"field": field},
        )
    return value


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        return datetime.min.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_clock() -> datetime:
    return datetime.now(UTC)


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path.expanduser().resolve(strict=False)))


def _require_generation_storage_isolated(
    paths: ManagedGenerationPaths,
    *,
    repository_root: str,
) -> None:
    managed = _normalized_path(paths.generation_root)
    repository = _normalized_path(Path(repository_root))
    try:
        common = os.path.commonpath((managed, repository))
    except ValueError:
        return
    if common in {managed, repository}:
        raise LifecycleOperationError(
            "Managed generation storage must be outside the target repository.",
            code="GENERATION_STORAGE_OVERLAP",
            details={
                "generation_root": str(paths.generation_root),
                "repository_root": repository_root,
            },
        )


def path_exists_safely(value: object) -> bool:
    return isinstance(value, Path) and os.path.lexists(value)
