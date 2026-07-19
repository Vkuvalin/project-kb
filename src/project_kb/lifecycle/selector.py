"""Explicit lifecycle selectors and immutable generation read descriptors."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from project_kb.errors import LifecycleOperationError
from project_kb.lifecycle.generation import GenerationDescriptor, LifecycleGenerationService
from project_kb.registry.db import open_existing_registry
from project_kb.resolver.repo_identity import repository_identity_hash
from project_kb.storage.home import resolve_home

_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SELECTOR_REQUEST_VERSION = 1


class LifecycleSelectorKind(StrEnum):
    SNAPSHOT_ID = "SNAPSHOT_ID"
    TASK_BASELINE = "TASK_BASELINE"
    TASK_LATEST_WORKING = "TASK_LATEST_WORKING"
    TASK_FINAL = "TASK_FINAL"
    PROJECT_BASELINE = "PROJECT_BASELINE"


class SnapshotSource(StrEnum):
    LEGACY_CANONICAL = "LEGACY_CANONICAL"
    LIFECYCLE_GENERATION = "LIFECYCLE_GENERATION"
    HISTORICAL_GENERATION = "HISTORICAL_GENERATION"


_TASK_POINTER_KINDS = frozenset(
    {
        LifecycleSelectorKind.TASK_BASELINE,
        LifecycleSelectorKind.TASK_LATEST_WORKING,
        LifecycleSelectorKind.TASK_FINAL,
    }
)


@dataclass(frozen=True)
class LifecycleSelectorRequest:
    """Versioned explicit request; no field is inferred from process state."""

    version: int
    kind: LifecycleSelectorKind
    project_id: str
    snapshot_id: str | None = None
    task_id: str | None = None
    expected_pointer_version: int | None = None


@dataclass(frozen=True)
class ResolvedGenerationDescriptor:
    """Pinned immutable lifecycle descriptor used after selector resolution."""

    selector_request_version: int
    selector_kind: LifecycleSelectorKind
    snapshot_id: str
    project_id: str
    workspace_id: str
    workspace_binding_generation: str
    task_id: str
    generation_sequence: int
    capture_purpose: str
    parent_snapshot_id: str | None
    origin_operation_id: str
    generation_state: str
    truth_claim: str
    relative_storage_path: str
    storage_layout_version: int
    path: Path
    file_size: int
    file_sha256: str
    semantic_schema_version: int
    capture_contract_fingerprint: str
    pointer_id: str | None
    pointer_kind: str | None
    resolved_pointer_version: int | None
    resolved_pointer_predecessor_snapshot_id: str | None
    snapshot_source: SnapshotSource
    captured_repo_root_norm: str
    captured_repository_identity_hash: str
    captured_git_head: str | None
    captured_git_branch: str | None
    captured_git_status_fingerprint: str
    captured_repository_evidence_json: str
    current_workspace_state: str | None
    current_workspace_root_norm: str | None
    current_workspace_binding_generation: str | None
    current_repository_identity_hash: str | None
    historical_binding_mismatch: bool
    historical_binding_reason: str | None
    resolution_timestamp: str
    descriptor_integrity_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": {
                "version": self.selector_request_version,
                "kind": self.selector_kind.value,
            },
            "snapshot_id": self.snapshot_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "workspace_binding_generation": self.workspace_binding_generation,
            "task_id": self.task_id,
            "generation_sequence": self.generation_sequence,
            "capture_purpose": self.capture_purpose,
            "parent_snapshot_id": self.parent_snapshot_id,
            "origin_operation_id": self.origin_operation_id,
            "generation_state": self.generation_state,
            "truth_claim": self.truth_claim,
            "managed_storage": {
                "relative_path": self.relative_storage_path,
                "resolved_path": str(self.path),
                "layout_version": self.storage_layout_version,
                "file_size": self.file_size,
                "file_sha256": self.file_sha256,
            },
            "semantic_schema_version": self.semantic_schema_version,
            "capture_contract_fingerprint": self.capture_contract_fingerprint,
            "pointer": (
                {
                    "pointer_id": self.pointer_id,
                    "kind": self.pointer_kind,
                    "resolved_version": self.resolved_pointer_version,
                    "predecessor_snapshot_id": (self.resolved_pointer_predecessor_snapshot_id),
                }
                if self.pointer_id is not None
                else None
            ),
            "snapshot_source": self.snapshot_source.value,
            "descriptor_integrity_hash": self.descriptor_integrity_hash,
            "captured_repository_evidence": json.loads(self.captured_repository_evidence_json),
            "current_binding": {
                "workspace_state": self.current_workspace_state,
                "workspace_root_norm": self.current_workspace_root_norm,
                "workspace_binding_generation": (self.current_workspace_binding_generation),
                "repository_identity_hash": self.current_repository_identity_hash,
                "historical_mismatch": self.historical_binding_mismatch,
                "historical_reason": self.historical_binding_reason,
            },
            "resolution_timestamp": self.resolution_timestamp,
        }


@dataclass(frozen=True)
class _ControlPlaneResolution:
    kind: LifecycleSelectorKind
    generation: Mapping[str, Any]
    workspace: Mapping[str, Any] | None
    task: Mapping[str, Any] | None
    pointer: Mapping[str, Any] | None


class LifecycleSelectorService:
    """Resolve aliases once, then validate only their pinned exact generation."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.home = (home or resolve_home()).resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.generations = LifecycleGenerationService(home=self.home)

    def resolve(
        self,
        request: LifecycleSelectorRequest,
    ) -> ResolvedGenerationDescriptor:
        kind = _validate_request(request)
        control = self._resolve_control_plane(request, kind)
        generation = dict(control.generation)
        _require_available_state(generation)

        try:
            exact = self.generations.resolve_generation(generation["snapshot_id"])
        except LifecycleOperationError as exc:
            _raise_mapped_generation_error(exc, kind=kind)
            raise AssertionError("mapped generation error did not raise") from exc
        _require_exact_identity(exact, generation)

        repository_evidence = _json_object(
            generation["repository_evidence_json"],
            field="repository_evidence_json",
        )
        workspace = dict(control.workspace) if control.workspace is not None else None
        current_identity = (
            repository_identity_hash(
                workspace["workspace_root_norm"],
                workspace["repository_fingerprint_json"],
            )
            if workspace is not None
            else None
        )
        historical_reason = _historical_reason(
            generation,
            repository_evidence,
            workspace,
            current_identity,
        )
        historical = historical_reason is not None
        if historical and kind is not LifecycleSelectorKind.SNAPSHOT_ID:
            raise LifecycleOperationError(
                "Managed pointer targets a generation from a historical workspace binding.",
                code="BROKEN_POINTER",
                details={
                    "selector_kind": kind.value,
                    "snapshot_id": generation["snapshot_id"],
                    "reason": historical_reason,
                },
            )

        pointer = dict(control.pointer) if control.pointer is not None else None
        source = (
            SnapshotSource.HISTORICAL_GENERATION
            if historical
            else SnapshotSource.LIFECYCLE_GENERATION
        )
        descriptor = ResolvedGenerationDescriptor(
            selector_request_version=request.version,
            selector_kind=kind,
            snapshot_id=exact.snapshot_id,
            project_id=exact.project_id,
            workspace_id=exact.workspace_id,
            workspace_binding_generation=exact.workspace_binding_generation,
            task_id=exact.task_id,
            generation_sequence=exact.generation_sequence,
            capture_purpose=exact.capture_purpose,
            parent_snapshot_id=generation["parent_snapshot_id"],
            origin_operation_id=exact.origin_operation_id,
            generation_state=exact.generation_state,
            truth_claim=generation["truth_claim"],
            relative_storage_path=exact.relative_storage_path,
            storage_layout_version=generation["storage_layout_version"],
            path=exact.path,
            file_size=exact.file_size,
            file_sha256=exact.file_sha256,
            semantic_schema_version=exact.schema_version,
            capture_contract_fingerprint=generation["capture_contract_fingerprint"],
            pointer_id=pointer["pointer_id"] if pointer is not None else None,
            pointer_kind=pointer["pointer_role"] if pointer is not None else None,
            resolved_pointer_version=(pointer["pointer_version"] if pointer is not None else None),
            resolved_pointer_predecessor_snapshot_id=(
                generation["parent_snapshot_id"]
                if pointer is not None
                and pointer["pointer_role"] in {"TASK_LATEST_WORKING", "TASK_FINAL"}
                else None
            ),
            snapshot_source=source,
            captured_repo_root_norm=str(repository_evidence["repo_root_norm"]),
            captured_repository_identity_hash=str(repository_evidence["repository_identity_hash"]),
            captured_git_head=_optional_string(repository_evidence.get("git_head")),
            captured_git_branch=_optional_string(repository_evidence.get("git_branch")),
            captured_git_status_fingerprint=str(repository_evidence["git_status_fingerprint"]),
            captured_repository_evidence_json=_canonical_json(repository_evidence),
            current_workspace_state=(workspace["workspace_state"] if workspace else None),
            current_workspace_root_norm=(workspace["workspace_root_norm"] if workspace else None),
            current_workspace_binding_generation=(
                workspace["workspace_binding_generation"] if workspace else None
            ),
            current_repository_identity_hash=current_identity,
            historical_binding_mismatch=historical,
            historical_binding_reason=historical_reason,
            resolution_timestamp=_iso_timestamp(self._clock()),
        )
        return replace(
            descriptor,
            descriptor_integrity_hash=_descriptor_integrity_hash(descriptor),
        )

    def resolve_reference(
        self,
        reference: LifecycleSelectorRequest | ResolvedGenerationDescriptor,
    ) -> ResolvedGenerationDescriptor:
        if isinstance(reference, LifecycleSelectorRequest):
            return self.resolve(reference)
        if not isinstance(reference, ResolvedGenerationDescriptor):
            raise LifecycleOperationError(
                "Lifecycle reads require an explicit selector or exact descriptor.",
                code="SELECTOR_REQUEST_INVALID",
            )
        self.revalidate_descriptor(reference)
        return reference

    def revalidate_descriptor(self, descriptor: ResolvedGenerationDescriptor) -> None:
        """Revalidate one exact ID without consulting its original alias again."""

        if (
            descriptor.selector_request_version != SELECTOR_REQUEST_VERSION
            or isinstance(descriptor.selector_request_version, bool)
            or not isinstance(descriptor.selector_kind, LifecycleSelectorKind)
        ):
            raise LifecycleOperationError(
                "Exact lifecycle descriptor selector provenance is invalid.",
                code="GENERATION_IDENTITY_MISMATCH",
                details={"snapshot_id": descriptor.snapshot_id},
            )
        _require_available_state(
            {
                "snapshot_id": descriptor.snapshot_id,
                "generation_state": descriptor.generation_state,
            }
        )
        _require_descriptor_integrity(descriptor)
        try:
            exact = self.generations.resolve_generation(descriptor.snapshot_id)
        except LifecycleOperationError as exc:
            _raise_mapped_generation_error(exc, kind=descriptor.selector_kind)
            raise AssertionError("mapped generation error did not raise") from exc
        expected = {
            "project_id": descriptor.project_id,
            "workspace_id": descriptor.workspace_id,
            "workspace_binding_generation": descriptor.workspace_binding_generation,
            "task_id": descriptor.task_id,
            "generation_sequence": descriptor.generation_sequence,
            "capture_purpose": descriptor.capture_purpose,
            "origin_operation_id": descriptor.origin_operation_id,
            "generation_state": descriptor.generation_state,
            "relative_storage_path": descriptor.relative_storage_path,
            "path": descriptor.path,
            "file_size": descriptor.file_size,
            "file_sha256": descriptor.file_sha256,
            "schema_version": descriptor.semantic_schema_version,
        }
        actual = {
            "project_id": exact.project_id,
            "workspace_id": exact.workspace_id,
            "workspace_binding_generation": exact.workspace_binding_generation,
            "task_id": exact.task_id,
            "generation_sequence": exact.generation_sequence,
            "capture_purpose": exact.capture_purpose,
            "origin_operation_id": exact.origin_operation_id,
            "generation_state": exact.generation_state,
            "relative_storage_path": exact.relative_storage_path,
            "path": exact.path,
            "file_size": exact.file_size,
            "file_sha256": exact.file_sha256,
            "schema_version": exact.schema_version,
        }
        if actual != expected:
            raise LifecycleOperationError(
                "Exact lifecycle descriptor no longer matches immutable generation evidence.",
                code="GENERATION_IDENTITY_MISMATCH",
                details={"snapshot_id": descriptor.snapshot_id},
            )
        with self._registry() as conn:
            generation = conn.execute(
                """SELECT parent_snapshot_id, truth_claim, storage_layout_version,
                          capture_contract_fingerprint, repository_evidence_json
                   FROM snapshot_generations
                   WHERE snapshot_id = ? AND project_id = ?""",
                (descriptor.snapshot_id, descriptor.project_id),
            ).fetchone()
            pointer = (
                conn.execute(
                    "SELECT * FROM managed_pointers WHERE pointer_id = ?",
                    (descriptor.pointer_id,),
                ).fetchone()
                if descriptor.pointer_id is not None
                else None
            )
        if generation is None:
            raise LifecycleOperationError(
                "Exact lifecycle descriptor registry evidence is unavailable.",
                code="GENERATION_IDENTITY_MISMATCH",
                details={"snapshot_id": descriptor.snapshot_id},
            )
        repository_evidence = _json_object(
            generation["repository_evidence_json"],
            field="repository_evidence_json",
        )
        registry_identity = (
            generation["parent_snapshot_id"],
            generation["truth_claim"],
            generation["storage_layout_version"],
            generation["capture_contract_fingerprint"],
            _canonical_json(repository_evidence),
            repository_evidence.get("repo_root_norm"),
            repository_evidence.get("repository_identity_hash"),
            repository_evidence.get("git_head"),
            repository_evidence.get("git_branch"),
            repository_evidence.get("git_status_fingerprint"),
        )
        descriptor_identity = (
            descriptor.parent_snapshot_id,
            descriptor.truth_claim,
            descriptor.storage_layout_version,
            descriptor.capture_contract_fingerprint,
            descriptor.captured_repository_evidence_json,
            descriptor.captured_repo_root_norm,
            descriptor.captured_repository_identity_hash,
            descriptor.captured_git_head,
            descriptor.captured_git_branch,
            descriptor.captured_git_status_fingerprint,
        )
        if registry_identity != descriptor_identity:
            raise LifecycleOperationError(
                "Exact lifecycle descriptor provenance does not match registry evidence.",
                code="GENERATION_IDENTITY_MISMATCH",
                details={"snapshot_id": descriptor.snapshot_id},
            )
        _require_descriptor_provenance(
            descriptor,
            dict(pointer) if pointer is not None else None,
        )

    def _resolve_control_plane(
        self,
        request: LifecycleSelectorRequest,
        kind: LifecycleSelectorKind,
    ) -> _ControlPlaneResolution:
        with self._registry() as conn:
            conn.execute("BEGIN")
            try:
                if kind is LifecycleSelectorKind.SNAPSHOT_ID:
                    generation = conn.execute(
                        """SELECT * FROM snapshot_generations
                           WHERE project_id = ? AND snapshot_id = ?""",
                        (request.project_id, request.snapshot_id),
                    ).fetchone()
                    if generation is None:
                        raise LifecycleOperationError(
                            "Exact lifecycle snapshot is not registered for this project.",
                            code="SNAPSHOT_NOT_FOUND",
                            details={
                                "project_id": request.project_id,
                                "snapshot_id": request.snapshot_id,
                            },
                        )
                    pointer = None
                else:
                    pointer = self._pointer_row(conn, request, kind)
                    generation = conn.execute(
                        "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                        (pointer["snapshot_id"],),
                    ).fetchone()
                    if generation is None:
                        raise LifecycleOperationError(
                            "Managed pointer target is not registered.",
                            code="BROKEN_POINTER",
                            details={
                                "pointer_id": pointer["pointer_id"],
                                "snapshot_id": pointer["snapshot_id"],
                            },
                        )
                    if generation["project_id"] != request.project_id or (
                        kind in _TASK_POINTER_KINDS and generation["task_id"] != request.task_id
                    ):
                        raise LifecycleOperationError(
                            "Managed pointer target ownership does not match its selector.",
                            code="BROKEN_POINTER",
                            details={"pointer_id": pointer["pointer_id"]},
                        )
                workspace = conn.execute(
                    """SELECT * FROM workspaces
                       WHERE workspace_id = ? AND project_id = ?""",
                    (generation["workspace_id"], generation["project_id"]),
                ).fetchone()
                task = conn.execute(
                    "SELECT * FROM lifecycle_tasks WHERE task_id = ?",
                    (generation["task_id"],),
                ).fetchone()
                if task is None or any(
                    (
                        task["project_id"] != generation["project_id"],
                        task["workspace_id"] != generation["workspace_id"],
                        task["workspace_binding_generation"]
                        != generation["workspace_binding_generation"],
                        task["capture_contract_fingerprint"]
                        != generation["capture_contract_fingerprint"],
                    )
                ):
                    raise LifecycleOperationError(
                        "Generation task ownership is unavailable.",
                        code="GENERATION_IDENTITY_MISMATCH",
                        details={"snapshot_id": generation["snapshot_id"]},
                    )
                result = _ControlPlaneResolution(
                    kind=kind,
                    generation=dict(generation),
                    workspace=dict(workspace) if workspace is not None else None,
                    task=dict(task),
                    pointer=dict(pointer) if pointer is not None else None,
                )
                conn.commit()
                return result
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    def _pointer_row(
        self,
        conn: Any,
        request: LifecycleSelectorRequest,
        kind: LifecycleSelectorKind,
    ) -> Any:
        if kind is LifecycleSelectorKind.PROJECT_BASELINE:
            row = conn.execute(
                """SELECT * FROM managed_pointers
                   WHERE project_id = ? AND task_id IS NULL
                     AND pointer_role = 'PROJECT_BASELINE'""",
                (request.project_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM managed_pointers
                   WHERE project_id = ? AND task_id = ? AND pointer_role = ?""",
                (request.project_id, request.task_id, kind.value),
            ).fetchone()
        if row is None:
            _raise_missing_pointer(kind, request)
        if row["pointer_version"] != request.expected_pointer_version:
            raise LifecycleOperationError(
                "Managed pointer version does not match the explicit selector request.",
                code="POINTER_VERSION_MISMATCH",
                details={
                    "selector_kind": kind.value,
                    "expected_pointer_version": request.expected_pointer_version,
                    "actual_pointer_version": row["pointer_version"],
                },
            )
        return row

    @contextmanager
    def _registry(self) -> Iterator[Any]:
        with open_existing_registry(
            home=self.home,
            now=lambda: _iso_timestamp(self._clock()),
            event_id=lambda: uuid.uuid4().hex,
        ) as conn:
            if conn is None:
                raise LifecycleOperationError(
                    "Lifecycle selector requires an existing registry.",
                    code="REGISTRY_NOT_FOUND",
                )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            yield conn


def _validate_request(request: LifecycleSelectorRequest) -> LifecycleSelectorKind:
    if not isinstance(request, LifecycleSelectorRequest):
        raise LifecycleOperationError(
            "Lifecycle selector request has an invalid type.",
            code="SELECTOR_REQUEST_INVALID",
        )
    if (
        not isinstance(request.version, int)
        or isinstance(request.version, bool)
        or request.version != SELECTOR_REQUEST_VERSION
    ):
        raise LifecycleOperationError(
            "Lifecycle selector request version is unsupported.",
            code="SELECTOR_VERSION_UNSUPPORTED",
            details={"version": request.version},
        )
    try:
        kind = LifecycleSelectorKind(request.kind)
    except ValueError as exc:
        raise LifecycleOperationError(
            "Lifecycle selector kind is unsupported.",
            code="SELECTOR_KIND_UNSUPPORTED",
            details={"kind": str(request.kind)},
        ) from exc
    _require_id(request.project_id, "project_id")
    if kind is LifecycleSelectorKind.SNAPSHOT_ID:
        _require_id(request.snapshot_id, "snapshot_id")
        if request.task_id is not None or request.expected_pointer_version is not None:
            _invalid_request(kind, "SNAPSHOT_ID forbids task and pointer fields")
    elif kind in _TASK_POINTER_KINDS:
        _require_id(request.task_id, "task_id")
        _require_pointer_version(request.expected_pointer_version)
        if request.snapshot_id is not None:
            _invalid_request(kind, "task pointer selectors forbid snapshot_id")
    else:
        _require_pointer_version(request.expected_pointer_version)
        if request.snapshot_id is not None or request.task_id is not None:
            _invalid_request(kind, "PROJECT_BASELINE forbids task_id and snapshot_id")
    return kind


def _descriptor_integrity_hash(descriptor: ResolvedGenerationDescriptor) -> str:
    payload: dict[str, Any] = {}
    for field in fields(descriptor):
        if field.name in {"descriptor_integrity_hash", "resolution_timestamp"}:
            continue
        value = getattr(descriptor, field.name)
        if isinstance(value, StrEnum):
            value = value.value
        elif isinstance(value, Path):
            value = str(value)
        payload[field.name] = value
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _descriptor_provenance_error(
    descriptor: ResolvedGenerationDescriptor,
    reason: str,
) -> None:
    raise LifecycleOperationError(
        "Exact lifecycle descriptor provenance is invalid or contradictory.",
        code="DESCRIPTOR_PROVENANCE_INVALID",
        details={"snapshot_id": descriptor.snapshot_id, "reason": reason},
    )


def _require_descriptor_integrity(descriptor: ResolvedGenerationDescriptor) -> None:
    if (
        not isinstance(descriptor.descriptor_integrity_hash, str)
        or _SHA256_PATTERN.fullmatch(descriptor.descriptor_integrity_hash) is None
        or not hmac.compare_digest(
            descriptor.descriptor_integrity_hash,
            _descriptor_integrity_hash(descriptor),
        )
    ):
        _descriptor_provenance_error(descriptor, "descriptor_integrity_mismatch")


def _require_descriptor_provenance(
    descriptor: ResolvedGenerationDescriptor,
    pointer: Mapping[str, Any] | None,
) -> None:
    kind = descriptor.selector_kind
    pointer_fields = (
        descriptor.pointer_id,
        descriptor.pointer_kind,
        descriptor.resolved_pointer_version,
        descriptor.resolved_pointer_predecessor_snapshot_id,
    )
    if kind is LifecycleSelectorKind.SNAPSHOT_ID:
        if any(value is not None for value in pointer_fields) or pointer is not None:
            _descriptor_provenance_error(descriptor, "exact_selector_has_pointer_provenance")
    else:
        if (
            not isinstance(descriptor.pointer_id, str)
            or _ID_PATTERN.fullmatch(descriptor.pointer_id) is None
            or descriptor.pointer_kind != kind.value
            or not isinstance(descriptor.resolved_pointer_version, int)
            or isinstance(descriptor.resolved_pointer_version, bool)
            or descriptor.resolved_pointer_version < 0
            or pointer is None
        ):
            _descriptor_provenance_error(descriptor, "alias_pointer_provenance_invalid")
        expected_task_id = (
            None if kind is LifecycleSelectorKind.PROJECT_BASELINE else descriptor.task_id
        )
        if any(
            (
                pointer["project_id"] != descriptor.project_id,
                pointer["task_id"] != expected_task_id,
                pointer["pointer_role"] != kind.value,
            )
        ):
            _descriptor_provenance_error(descriptor, "alias_pointer_ownership_mismatch")
        if pointer["snapshot_id"] == descriptor.snapshot_id:
            if pointer["pointer_version"] != descriptor.resolved_pointer_version:
                _descriptor_provenance_error(descriptor, "alias_pointer_version_mismatch")
        elif (
            kind
            not in {
                LifecycleSelectorKind.TASK_LATEST_WORKING,
                LifecycleSelectorKind.PROJECT_BASELINE,
            }
            or pointer["pointer_version"] <= descriptor.resolved_pointer_version
        ):
            _descriptor_provenance_error(descriptor, "alias_pointer_movement_invalid")

    expected_capture_purpose: str | None
    expected_predecessor: str | None
    if kind is LifecycleSelectorKind.TASK_BASELINE:
        expected_capture_purpose = "TASK_BASELINE"
        expected_predecessor = None
        if descriptor.generation_sequence != 0 or descriptor.parent_snapshot_id is not None:
            _descriptor_provenance_error(descriptor, "task_baseline_generation_invalid")
    elif kind is LifecycleSelectorKind.TASK_LATEST_WORKING:
        expected_capture_purpose = "TASK_WORKING"
        expected_predecessor = descriptor.parent_snapshot_id
    elif kind is LifecycleSelectorKind.TASK_FINAL:
        expected_capture_purpose = "TASK_FINAL"
        expected_predecessor = descriptor.parent_snapshot_id
    elif kind is LifecycleSelectorKind.PROJECT_BASELINE:
        expected_capture_purpose = "TASK_FINAL"
        expected_predecessor = None
    else:
        expected_capture_purpose = None
        expected_predecessor = None
    if expected_capture_purpose is not None and (
        descriptor.capture_purpose != expected_capture_purpose
        or descriptor.resolved_pointer_predecessor_snapshot_id != expected_predecessor
        or (
            kind
            in {
                LifecycleSelectorKind.TASK_LATEST_WORKING,
                LifecycleSelectorKind.TASK_FINAL,
            }
            and (
                descriptor.generation_sequence <= 0
                or descriptor.parent_snapshot_id is None
                or _ID_PATTERN.fullmatch(descriptor.parent_snapshot_id) is None
            )
        )
    ):
        _descriptor_provenance_error(descriptor, "alias_generation_provenance_invalid")

    if descriptor.snapshot_source not in {
        SnapshotSource.LIFECYCLE_GENERATION,
        SnapshotSource.HISTORICAL_GENERATION,
    }:
        _descriptor_provenance_error(descriptor, "snapshot_source_invalid")
    if descriptor.historical_binding_mismatch:
        current_matches = all(
            (
                descriptor.current_workspace_state == "ACTIVE",
                descriptor.current_workspace_root_norm == descriptor.captured_repo_root_norm,
                descriptor.current_workspace_binding_generation
                == descriptor.workspace_binding_generation,
                descriptor.current_repository_identity_hash
                == descriptor.captured_repository_identity_hash,
            )
        )
        if (
            descriptor.snapshot_source is not SnapshotSource.HISTORICAL_GENERATION
            or not descriptor.historical_binding_reason
            or current_matches
        ):
            _descriptor_provenance_error(descriptor, "historical_source_provenance_invalid")
    elif any(
        (
            descriptor.snapshot_source is not SnapshotSource.LIFECYCLE_GENERATION,
            descriptor.historical_binding_reason is not None,
            descriptor.current_workspace_state != "ACTIVE",
            descriptor.current_workspace_root_norm != descriptor.captured_repo_root_norm,
            descriptor.current_workspace_binding_generation
            != descriptor.workspace_binding_generation,
            descriptor.current_repository_identity_hash
            != descriptor.captured_repository_identity_hash,
        )
    ):
        _descriptor_provenance_error(descriptor, "active_source_provenance_invalid")


def _invalid_request(kind: LifecycleSelectorKind, reason: str) -> None:
    raise LifecycleOperationError(
        "Lifecycle selector request is invalid.",
        code="SELECTOR_REQUEST_INVALID",
        details={"selector_kind": kind.value, "reason": reason},
    )


def _require_pointer_version(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LifecycleOperationError(
            "Alias selectors require an exact non-negative pointer version.",
            code="SELECTOR_REQUEST_INVALID",
            details={"field": "expected_pointer_version"},
        )


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise LifecycleOperationError(
            "Lifecycle selector identity is invalid.",
            code="SELECTOR_REQUEST_INVALID",
            details={"field": field},
        )
    return value


def _raise_missing_pointer(
    kind: LifecycleSelectorKind,
    request: LifecycleSelectorRequest,
) -> None:
    codes = {
        LifecycleSelectorKind.TASK_LATEST_WORKING: "TASK_WORKING_NOT_AVAILABLE",
        LifecycleSelectorKind.TASK_FINAL: "TASK_FINAL_NOT_AVAILABLE",
        LifecycleSelectorKind.PROJECT_BASELINE: "PROJECT_BASELINE_NOT_AVAILABLE",
    }
    raise LifecycleOperationError(
        "Requested lifecycle pointer is not available.",
        code=codes.get(kind, "POINTER_NOT_FOUND"),
        details={
            "selector_kind": kind.value,
            "project_id": request.project_id,
            "task_id": request.task_id,
        },
    )


def _require_available_state(generation: Mapping[str, Any]) -> None:
    state = generation["generation_state"]
    if state == "AVAILABLE":
        return
    code = {
        "DELETED": "GENERATION_DELETED",
        "QUARANTINED": "GENERATION_QUARANTINED",
    }.get(state, "GENERATION_NOT_AVAILABLE")
    raise LifecycleOperationError(
        "Lifecycle generation is not available for ordinary reads or comparison.",
        code=code,
        details={"snapshot_id": generation["snapshot_id"], "generation_state": state},
    )


def _raise_mapped_generation_error(
    error: LifecycleOperationError,
    *,
    kind: LifecycleSelectorKind,
) -> None:
    if error.code == "GENERATION_NOT_FOUND":
        code = (
            "SNAPSHOT_NOT_FOUND" if kind is LifecycleSelectorKind.SNAPSHOT_ID else "BROKEN_POINTER"
        )
    elif error.code == "GENERATION_MISSING":
        code = "GENERATION_FILE_MISSING"
    elif error.code == "GENERATION_CORRUPT":
        code = (
            "GENERATION_IDENTITY_MISMATCH"
            if "identity" in error.message.casefold()
            else "GENERATION_INTEGRITY_FAILED"
        )
    elif error.code in {"GENERATION_STATE_BLOCKED", "GENERATION_STATE_CHANGED"}:
        state = error.details.get("generation_state") or error.details.get("current_state")
        code = {
            "DELETED": "GENERATION_DELETED",
            "QUARANTINED": "GENERATION_QUARANTINED",
        }.get(state, "GENERATION_NOT_AVAILABLE")
    else:
        raise error
    raise LifecycleOperationError(
        error.message,
        code=code,
        retryable=error.retryable,
        details=error.details,
    ) from error


def _require_exact_identity(
    exact: GenerationDescriptor,
    generation: Mapping[str, Any],
) -> None:
    expected = (
        generation["snapshot_id"],
        generation["project_id"],
        generation["workspace_id"],
        generation["workspace_binding_generation"],
        generation["task_id"],
        generation["generation_sequence"],
        generation["capture_purpose"],
        generation["origin_operation_id"],
        generation["generation_state"],
        generation["relative_storage_path"],
        generation["file_size"],
        generation["file_sha256"],
    )
    actual = (
        exact.snapshot_id,
        exact.project_id,
        exact.workspace_id,
        exact.workspace_binding_generation,
        exact.task_id,
        exact.generation_sequence,
        exact.capture_purpose,
        exact.origin_operation_id,
        exact.generation_state,
        exact.relative_storage_path,
        exact.file_size,
        exact.file_sha256,
    )
    if actual != expected:
        raise LifecycleOperationError(
            "Exact generation identity changed after selector resolution.",
            code="GENERATION_IDENTITY_MISMATCH",
            details={"snapshot_id": generation["snapshot_id"]},
        )


def _historical_reason(
    generation: Mapping[str, Any],
    repository_evidence: Mapping[str, Any],
    workspace: Mapping[str, Any] | None,
    current_identity: str | None,
) -> str | None:
    if workspace is None:
        return "workspace_registration_missing"
    if workspace["workspace_state"] != "ACTIVE":
        return f"workspace_state_{str(workspace['workspace_state']).casefold()}"
    if workspace["workspace_binding_generation"] != generation["workspace_binding_generation"]:
        return "workspace_binding_generation_changed"
    if workspace["workspace_root_norm"] != repository_evidence["repo_root_norm"]:
        return "workspace_root_changed"
    if current_identity != repository_evidence["repository_identity_hash"]:
        return "repository_identity_changed"
    return None


def _json_object(value: object, *, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise LifecycleOperationError(
            "Lifecycle registry evidence is invalid JSON.",
            code="GENERATION_IDENTITY_MISMATCH",
            details={"field": field},
        ) from exc
    if not isinstance(payload, dict):
        raise LifecycleOperationError(
            "Lifecycle registry evidence is not an object.",
            code="GENERATION_IDENTITY_MISMATCH",
            details={"field": field},
        )
    return payload


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
