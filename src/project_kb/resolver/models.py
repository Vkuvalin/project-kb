"""Typed status models for the Stage 3 JSON contract."""

from dataclasses import asdict, dataclass, field
from typing import Any

from project_kb.errors import ProjectKbError
from project_kb.gating.models import RecommendedAction
from project_kb.registry.models import ProjectRecord
from project_kb.resolver.state import ProjectState


@dataclass(frozen=True)
class Resolution:
    mode: str
    status: str
    project_name_input: str | None
    working_directory: str
    resolved_repo_root: str | None
    resolved_by: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepoCheck:
    status: str
    stored_repo_root: str | None
    current_git_root: str | None
    path_exists: bool | None
    is_directory: bool | None
    is_git_repository: bool | None
    git_root_matches: bool | None
    fingerprint_status: str
    fingerprint_strength: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def not_checked(cls, reason: str) -> RepoCheck:
        return cls(
            status="not_checked",
            stored_repo_root=None,
            current_git_root=None,
            path_exists=None,
            is_directory=None,
            is_git_repository=None,
            git_root_matches=None,
            fingerprint_status="not_checked",
            fingerprint_strength=None,
            reason=reason,
        )


@dataclass(frozen=True)
class StorageCheck:
    status: str
    expected_storage_path: str | None
    actual_storage_path: str | None
    storage_exists: bool | None
    is_directory: bool | None
    exports_exists: bool | None
    runs_exists: bool | None
    missing_paths: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["missing_paths"] = list(self.missing_paths)
        return payload

    @classmethod
    def not_checked(cls, reason: str) -> StorageCheck:
        return cls(
            status="not_checked",
            expected_storage_path=None,
            actual_storage_path=None,
            storage_exists=None,
            is_directory=None,
            exports_exists=None,
            runs_exists=None,
            missing_paths=(),
            reason=reason,
        )


@dataclass(frozen=True)
class SnapshotCheck:
    status: str
    snapshot_id: str | None
    indexed_at: str | None
    git_commit_at_index: str | None
    current_git_commit: str | None
    is_current: bool | None
    reason: str
    availability: str = "AVAILABLE"
    compatibility: str = "COMPATIBLE"
    currentness: str = "UNVERIFIED"
    truth_claim: str | None = None
    verification_mode: str | None = None
    verified_at: str | None = None
    verification_duration_ms: int | None = None
    verification_timings_ms: dict[str, int] = field(default_factory=dict)
    verification_attempts: int = 0
    mismatch_paths: tuple[str, ...] = ()
    deltas: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    exclusions: tuple[str, ...] = ()
    verification_scope: dict[str, Any] = field(default_factory=dict)
    proof_contract_version: str | None = None
    verifier_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.is_current is None:
            payload.pop("is_current")
        payload["mismatch_paths"] = list(self.mismatch_paths)
        payload["deltas"] = list(self.deltas)
        payload["diagnostics"] = list(self.diagnostics)
        payload["exclusions"] = list(self.exclusions)
        return payload

    @classmethod
    def absent(cls) -> SnapshotCheck:
        return cls(
            status="absent",
            snapshot_id=None,
            indexed_at=None,
            git_commit_at_index=None,
            current_git_commit=None,
            is_current=False,
            reason="snapshot_not_created",
            availability="MISSING",
            compatibility="NOT_APPLICABLE",
        )

    @classmethod
    def not_checked(
        cls,
        reason: str,
        *,
        availability: str = "MISSING",
        compatibility: str = "NOT_CHECKED",
    ) -> SnapshotCheck:
        return cls(
            status="not_checked",
            snapshot_id=None,
            indexed_at=None,
            git_commit_at_index=None,
            current_git_commit=None,
            is_current=False,
            reason=reason,
            availability=availability,
            compatibility=compatibility,
        )


@dataclass(frozen=True)
class Availability:
    can_use_project: bool
    can_use_snapshot: bool
    can_search: bool
    can_generate_exports: bool
    can_generate_context: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    @classmethod
    def unavailable(cls) -> Availability:
        return cls(False, False, False, False, False)


@dataclass(frozen=True)
class StatusOutcome:
    ok: bool
    result: str
    code: str
    message: str
    exit_code: int
    project_state: ProjectState
    resolution: Resolution
    project: ProjectRecord | None
    repo_check: RepoCheck
    storage_check: StorageCheck
    snapshot_check: SnapshotCheck
    availability: Availability
    requires_user_action: bool
    recommended_action: RecommendedAction
    error: ProjectKbError | None

    def data(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.to_dict(),
            "project": self.project.to_dict() if self.project else None,
            "project_state": self.project_state.value,
            "repo_check": self.repo_check.to_dict(),
            "storage_check": self.storage_check.to_dict(),
            "snapshot_check": self.snapshot_check.to_dict(),
            "availability": self.availability.to_dict(),
            "requires_user_action": self.requires_user_action,
            "recommended_action": self.recommended_action.to_dict(),
        }
