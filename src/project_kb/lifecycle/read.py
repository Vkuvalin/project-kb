"""Explicit lifecycle-generation reads, currentness checks, and gate adaptation."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project_kb.errors import LifecycleOperationError, SnapshotQueryError
from project_kb.gating import GateContext, GateRequirement, GateResult, evaluate_gate
from project_kb.indexing.query import SnapshotQueryAdapter
from project_kb.lifecycle.selector import (
    LifecycleSelectorRequest,
    LifecycleSelectorService,
    ResolvedGenerationDescriptor,
    SnapshotSource,
)
from project_kb.registry.db import open_existing_registry
from project_kb.resolver.repo_identity import repository_identity_hash
from project_kb.snapshot.currentness import (
    CurrentnessResult,
    CurrentnessState,
    RepositoryBindingObservation,
    VerificationMode,
    observe_repository_binding,
    verify_snapshot_currentness,
)
from project_kb.snapshot.database import SnapshotReader
from project_kb.storage.home import resolve_home

LifecycleReference = LifecycleSelectorRequest | ResolvedGenerationDescriptor


@dataclass(frozen=True)
class LifecycleCurrentnessOutcome:
    descriptor: ResolvedGenerationDescriptor
    applicable: bool
    reason: str
    result: CurrentnessResult | None

    @property
    def state(self) -> str:
        if self.result is None:
            return "NOT_APPLICABLE"
        return self.result.state.value

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "applicable": self.applicable,
            "reason": self.reason,
            "state": self.state,
            "snapshot_source": self.descriptor.snapshot_source.value,
            "snapshot_id": self.descriptor.snapshot_id,
        }
        if self.result is not None:
            payload["verification"] = {
                "mode": self.result.mode.value,
                "reason": self.result.reason,
                "current_git_commit": self.result.current_git_commit,
                "verified_at": self.result.verified_at,
                "mismatch_paths": list(self.result.mismatch_paths),
                "deltas": list(self.result.deltas),
                "diagnostics": list(self.result.diagnostics),
                "exclusions": list(self.result.exclusions),
                "attempts": self.result.attempts,
                "timings_ms": self.result.timings_ms,
                "binding_status": self.result.binding_status,
            }
        return payload


@dataclass(frozen=True)
class LifecycleGateOutcome:
    descriptor: ResolvedGenerationDescriptor
    result: GateResult
    binding_reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_source": self.descriptor.snapshot_source.value,
            "snapshot_id": self.descriptor.snapshot_id,
            "binding_reason_code": self.binding_reason_code,
            "gate": self.result.to_dict(),
        }


class LifecycleReadService:
    """Read one exact lifecycle generation without moving or publishing pointers."""

    def __init__(self, *, home: Path | None = None) -> None:
        self.home = (home or resolve_home()).resolve()
        self.selectors = LifecycleSelectorService(home=self.home)

    def resolve(self, reference: LifecycleReference) -> ResolvedGenerationDescriptor:
        return self.selectors.resolve_reference(reference)

    def symbols(
        self,
        reference: LifecycleReference,
        *,
        file: str | None = None,
        name: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self._adapter(reference).symbols(file=file, name=name)

    def imports(
        self,
        reference: LifecycleReference,
        *,
        file: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self._adapter(reference).imports(file=file)

    def inspect(
        self,
        reference: LifecycleReference,
        *,
        file: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._adapter(reference).inspect(file=file)

    def currentness(
        self,
        reference: LifecycleReference,
        *,
        mode: VerificationMode | str,
        max_attempts: int = 2,
    ) -> LifecycleCurrentnessOutcome:
        try:
            verification_mode = VerificationMode(mode)
        except ValueError as exc:
            raise LifecycleOperationError(
                "Lifecycle currentness mode is invalid.",
                code="CURRENTNESS_REQUEST_INVALID",
            ) from exc
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 5
        ):
            raise LifecycleOperationError(
                "Lifecycle currentness attempts are outside the supported bounds.",
                code="CURRENTNESS_REQUEST_INVALID",
                details={"max_attempts": 5},
            )
        descriptor = self.resolve(reference)
        if descriptor.snapshot_source is SnapshotSource.HISTORICAL_GENERATION:
            return LifecycleCurrentnessOutcome(
                descriptor=descriptor,
                applicable=False,
                reason="historical_generation_has_no_active_repository_binding",
                result=None,
            )

        workspace = self._workspace(descriptor)
        repo_root = Path(workspace["workspace_root_norm"])

        def active_binding() -> RepositoryBindingObservation:
            current = self._workspace(descriptor)
            return _observe_workspace(current)

        reader = self._reader(descriptor)
        verification_arguments = {
            "repo_root": repo_root,
            "snapshot_meta": reader.meta,
            "mode": verification_mode,
            "active_binding": active_binding,
            "expected_binding": (
                descriptor.captured_repo_root_norm,
                descriptor.captured_repository_identity_hash,
                descriptor.workspace_binding_generation,
            ),
            "max_attempts": max_attempts,
        }
        if verification_mode is VerificationMode.FAST:
            result = verify_snapshot_currentness(
                descriptor.path,
                **verification_arguments,
            )
            return LifecycleCurrentnessOutcome(
                descriptor=descriptor,
                applicable=True,
                reason=result.reason,
                result=result,
            )
        scratch_parent = self.home / "scratch" / "currentness"
        scratch_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="verify-",
            dir=scratch_parent,
        ) as temporary_root:
            result = verify_snapshot_currentness(
                descriptor.path,
                temporary_index_root=Path(temporary_root),
                **verification_arguments,
            )
        return LifecycleCurrentnessOutcome(
            descriptor=descriptor,
            applicable=True,
            reason=result.reason,
            result=result,
        )

    def gate(
        self,
        reference: LifecycleReference,
        requirements: Sequence[GateRequirement],
        *,
        currentness: LifecycleCurrentnessOutcome | None = None,
        user_approval: bool = False,
    ) -> LifecycleGateOutcome:
        if (
            not isinstance(user_approval, bool)
            or not isinstance(requirements, Sequence)
            or isinstance(requirements, (str, bytes))
            or any(not isinstance(item, GateRequirement) for item in requirements)
            or (
                currentness is not None and not isinstance(currentness, LifecycleCurrentnessOutcome)
            )
        ):
            raise LifecycleOperationError(
                "Lifecycle gate context is invalid.",
                code="GATE_CONTEXT_INVALID",
            )
        descriptor = self.resolve(reference)
        if currentness is not None:
            self.selectors.revalidate_descriptor(currentness.descriptor)
            if (
                currentness.descriptor.snapshot_id != descriptor.snapshot_id
                or currentness.descriptor.descriptor_integrity_hash
                != descriptor.descriptor_integrity_hash
            ):
                raise LifecycleOperationError(
                    "Currentness evidence does not match the exact descriptor identity.",
                    code="CURRENTNESS_EVIDENCE_MISMATCH",
                    details={"snapshot_id": descriptor.snapshot_id},
                )
        binding_valid, binding_reason_code = self._gate_binding_status(descriptor)
        currentness_state = (
            currentness.result.state.value
            if binding_valid and currentness is not None and currentness.result is not None
            else CurrentnessState.UNVERIFIED.value
        )
        result = evaluate_gate(
            GateContext(
                registry_available=True,
                project_resolved=True,
                repo_valid=binding_valid,
                storage_valid=True,
                snapshot_present=True,
                snapshot_currentness=currentness_state,
                user_approval=user_approval,
            ),
            requirements,
        )
        return LifecycleGateOutcome(
            descriptor=descriptor,
            result=result,
            binding_reason_code=binding_reason_code,
        )

    def _adapter(self, reference: LifecycleReference) -> SnapshotQueryAdapter:
        descriptor = self.resolve(reference)
        reader = self._reader(descriptor)
        return SnapshotQueryAdapter(reader, _read_context(descriptor, reader.meta))

    def _reader(self, descriptor: ResolvedGenerationDescriptor) -> SnapshotReader:
        active_binding = None
        if descriptor.snapshot_source is SnapshotSource.LIFECYCLE_GENERATION:

            def active_binding() -> tuple[str, str, str]:
                workspace = self._workspace(descriptor)
                return (
                    workspace["workspace_root_norm"],
                    repository_identity_hash(
                        workspace["workspace_root_norm"],
                        workspace["repository_fingerprint_json"],
                    ),
                    workspace["workspace_binding_generation"],
                )

        return SnapshotReader(
            descriptor.path,
            project_id=descriptor.project_id,
            expected_repo_root_norm=descriptor.captured_repo_root_norm,
            expected_repository_identity_hash=(descriptor.captured_repository_identity_hash),
            expected_repository_binding_generation=(descriptor.workspace_binding_generation),
            active_binding=active_binding,
        )

    def _workspace(self, descriptor: ResolvedGenerationDescriptor) -> Mapping[str, Any]:
        with open_existing_registry(
            home=self.home,
            now=_utc_now,
            event_id=lambda: uuid.uuid4().hex,
        ) as conn:
            if conn is None:
                raise LifecycleOperationError(
                    "Lifecycle read requires an existing registry.",
                    code="REGISTRY_NOT_FOUND",
                )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                """SELECT * FROM workspaces
                   WHERE workspace_id = ? AND project_id = ?""",
                (descriptor.workspace_id, descriptor.project_id),
            ).fetchone()
        if row is None:
            raise SnapshotQueryError(
                "Lifecycle workspace binding is no longer registered.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={"reason": "workspace_registration_missing"},
            )
        workspace = dict(row)
        if workspace["workspace_state"] != "ACTIVE":
            raise SnapshotQueryError(
                "Lifecycle workspace binding is no longer active.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={
                    "reason": "workspace_not_active",
                    "workspace_state": workspace["workspace_state"],
                },
            )
        if workspace["workspace_binding_generation"] != descriptor.workspace_binding_generation:
            raise SnapshotQueryError(
                "Lifecycle workspace binding changed during snapshot access.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={"reason": "workspace_binding_generation_changed"},
            )
        if workspace["workspace_root_norm"] != descriptor.captured_repo_root_norm:
            raise SnapshotQueryError(
                "Lifecycle workspace root changed during snapshot access.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={"reason": "workspace_root_changed"},
            )
        if workspace["repository_fingerprint_json"] is None:
            raise SnapshotQueryError(
                "Lifecycle workspace identity evidence is unavailable.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={"reason": "workspace_fingerprint_missing"},
            )
        current_identity = repository_identity_hash(
            workspace["workspace_root_norm"],
            workspace["repository_fingerprint_json"],
        )
        if current_identity != descriptor.captured_repository_identity_hash:
            raise SnapshotQueryError(
                "Lifecycle workspace repository identity changed during snapshot access.",
                code="SNAPSHOT_REBUILD_REQUIRED",
                details={"reason": "repository_identity_changed"},
            )
        return workspace

    def _gate_binding_status(
        self,
        descriptor: ResolvedGenerationDescriptor,
    ) -> tuple[bool, str]:
        if descriptor.snapshot_source is SnapshotSource.HISTORICAL_GENERATION:
            return False, "HISTORICAL_BINDING"
        try:
            self._workspace(descriptor)
        except SnapshotQueryError as exc:
            reason = str(exc.details.get("reason", "binding_unavailable"))
            return False, reason.upper()
        return True, "ACTIVE_BINDING_MATCH"


def _read_context(
    descriptor: ResolvedGenerationDescriptor,
    snapshot_meta: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "snapshot_source": descriptor.snapshot_source.value,
        "selector_kind": descriptor.selector_kind.value,
        "snapshot": {
            "snapshot_id": descriptor.snapshot_id,
            "snapshot_source": descriptor.snapshot_source.value,
            "created_at": snapshot_meta["created_at"],
            "schema_version": snapshot_meta["schema_version"],
            "scanner_version": snapshot_meta["scanner_version"],
            "policy_version": snapshot_meta["policy_version"],
            "extractor_versions": snapshot_meta["extractor_versions"],
            "compatibility": snapshot_meta["compatibility"],
        },
        "generation": descriptor.to_dict(),
        "query_warnings": (
            [
                {
                    "code": "HISTORICAL_GENERATION",
                    "message": (
                        "The exact generation is readable, but it no longer has "
                        "the active workspace binding."
                    ),
                }
            ]
            if descriptor.snapshot_source is SnapshotSource.HISTORICAL_GENERATION
            else []
        ),
    }


def _observe_workspace(workspace: Mapping[str, Any]) -> RepositoryBindingObservation:
    fingerprint = workspace["repository_fingerprint_json"]
    if not isinstance(fingerprint, str):
        raise ValueError("active workspace fingerprint is unavailable")
    return observe_repository_binding(
        repo_root=Path(workspace["workspace_root_norm"]),
        repo_root_norm=workspace["workspace_root_norm"],
        repository_fingerprint_json=fingerprint,
        repository_binding_generation=workspace["workspace_binding_generation"],
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
