"""Publisher-neutral trusted repository capture and semantic snapshot sealing."""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project_kb.errors import IndexingError
from project_kb.indexing.identity import OCCURRENCE_CONTRACT_VERSION
from project_kb.indexing.models import RepoState, ScanPolicy
from project_kb.indexing.module_map import MODULE_MAP_VERSION
from project_kb.indexing.scanner import (
    RepositoryChangedError,
    capture_repo_state,
    git_candidates,
    scan_repository,
    verify_scan_evidence,
)
from project_kb.snapshot.database import (
    EXPECTED_EXTRACTORS,
    PROOF_CONTRACT_VERSION,
    SCANNER_VERSION,
    SCHEMA_VERSION,
    VERIFIER_VERSION,
    validate_snapshot,
    write_snapshot,
)


@dataclass(frozen=True)
class CaptureWorkspace:
    """Explicit physical-workspace ownership evidence for one capture."""

    project_id: str
    workspace_id: str
    repo_root_norm: str
    repository_identity_hash: str
    binding_generation: str


@dataclass(frozen=True)
class CaptureContract:
    """Versioned trusted-capture inputs pinned by a caller."""

    policy: ScanPolicy
    schema_version: int
    scanner_version: str
    extractor_versions: tuple[tuple[str, str], ...]
    proof_contract_version: str
    verifier_version: str
    module_map_version: str
    occurrence_contract_version: str

    @classmethod
    def current(cls, policy: ScanPolicy) -> CaptureContract:
        return cls(
            policy=policy,
            schema_version=SCHEMA_VERSION,
            scanner_version=SCANNER_VERSION,
            extractor_versions=tuple(sorted(EXPECTED_EXTRACTORS.items())),
            proof_contract_version=PROOF_CONTRACT_VERSION,
            verifier_version=VERIFIER_VERSION,
            module_map_version=MODULE_MAP_VERSION,
            occurrence_contract_version=OCCURRENCE_CONTRACT_VERSION,
        )


@dataclass(frozen=True)
class CaptureAttempt:
    """Caller-owned identity and temporary destination for one bounded attempt."""

    artifact_path: Path
    snapshot_id: str
    run_id: str


@dataclass(frozen=True)
class CaptureRetryEvent:
    """Narrow retry notification for caller-owned ancillary behavior."""

    attempt: CaptureAttempt
    attempt_number: int
    publication_state: str
    error: RepositoryChangedError


@dataclass(frozen=True)
class CaptureRequest:
    """All non-global identities, contracts, and bounded attempts for one capture."""

    workspace: CaptureWorkspace
    repository_root: Path
    attempts: tuple[CaptureAttempt, ...]
    contract: CaptureContract
    workspace_matches_repository: Callable[[Path, CaptureWorkspace], bool]


@dataclass(frozen=True)
class SealedArtifactDescriptor:
    """Closed, validated semantic-v2 artifact with no publication side effects."""

    artifact_path: Path
    workspace: CaptureWorkspace
    snapshot_id: str
    run_id: str
    created_at: str
    repository_state_before: RepoState
    repository_state_after: RepoState
    repository_state_final: RepoState
    schema_version: int
    scanner_version: str
    policy_version: str
    extractor_versions: tuple[tuple[str, str], ...]
    proof_contract_version: str
    verifier_version: str
    module_map_version: str
    occurrence_contract_version: str
    logical_fingerprint: str
    observation_fingerprint: str
    size_bytes: int
    counts: dict[str, int]
    timings: dict[str, int]
    warnings: tuple[dict[str, Any], ...]
    started_at: str
    started_perf_ns: int


@dataclass(frozen=True)
class _CaptureArtifactOwnership:
    """Filesystem leaves proven absent before and owned during one attempt."""

    paths: tuple[Path, ...]


def capture_trusted_artifact(
    request: CaptureRequest,
    *,
    retry_observer: Callable[[CaptureRetryEvent], None] | None = None,
) -> SealedArtifactDescriptor:
    """Run bounded attempts and return one publisher-neutral sealed artifact."""

    try:
        _require_current_contract(request.contract)
        _preflight_capture_request(request)
    except BaseException as exc:
        exc.capture_artifact_owned = False  # type: ignore[attr-defined]
        raise
    for attempt_number, attempt in enumerate(request.attempts, start=1):
        try:
            ownership = _claim_capture_artifact(attempt)
            return _capture_attempt(request, attempt, ownership)
        except RepositoryChangedError as exc:
            _annotate_attempt(exc, attempt, attempt_number)
            event = CaptureRetryEvent(
                attempt=attempt,
                attempt_number=attempt_number,
                publication_state=_failure_state(exc),
                error=exc,
            )
            if retry_observer is not None:
                try:
                    retry_observer(event)
                except BaseException as observer_error:
                    observer_error.capture_state = event.publication_state  # type: ignore[attr-defined]
                    _annotate_attempt(observer_error, attempt, attempt_number)
                    raise
            if attempt_number < len(request.attempts):
                continue
            raise
        except BaseException as exc:
            _annotate_attempt(exc, attempt, attempt_number)
            raise
    raise AssertionError("bounded capture attempts did not return or raise")


def _capture_attempt(
    request: CaptureRequest,
    attempt: CaptureAttempt,
    ownership: _CaptureArtifactOwnership,
) -> SealedArtifactDescriptor:
    """Build, validate, terminally seal, and close one capture attempt."""

    start_ns = time.perf_counter_ns()
    started_at = _precise_utc_now()
    capture_state = "BUILDING"
    try:
        if not request.workspace_matches_repository(
            request.repository_root,
            request.workspace,
        ):
            raise RepositoryChangedError(
                "repository root or identity does not match the capture workspace"
            )
        enumeration_start = time.perf_counter_ns()
        candidates = git_candidates(request.repository_root, request.contract.policy)
        before = capture_repo_state(
            request.repository_root,
            candidates,
            request.contract.policy,
        )
        enumeration_ms = (time.perf_counter_ns() - enumeration_start) // 1_000_000
        facts = scan_repository(
            request.repository_root,
            candidates,
            request.contract.policy,
            snapshot_id=attempt.snapshot_id,
        )
        facts.timings["enumeration_ms"] = enumeration_ms
        evidence_stable = verify_scan_evidence(
            request.repository_root,
            facts,
            request.contract.policy,
        )
        after_candidates = git_candidates(request.repository_root, request.contract.policy)
        after = capture_repo_state(
            request.repository_root,
            after_candidates,
            request.contract.policy,
        )
        if (
            before != after
            or not before.visibility_sealed
            or not after.visibility_sealed
            or not evidence_stable
        ):
            raise RepositoryChangedError(
                "repository candidates or processed objects changed after extraction"
            )

        warnings = _warnings(facts)
        finished_at = _precise_utc_now()
        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        (
            logical_fingerprint,
            observation_fingerprint,
            snapshot_size,
            database_write_ms,
        ) = write_snapshot(
            attempt.artifact_path,
            snapshot_id=attempt.snapshot_id,
            run_id=attempt.run_id,
            project_id=request.workspace.project_id,
            repo_root_norm=request.workspace.repo_root_norm,
            repository_identity_hash=request.workspace.repository_identity_hash,
            repository_binding_generation=request.workspace.binding_generation,
            created_at=finished_at,
            started_at=started_at,
            finished_at=finished_at,
            before=before,
            after=after,
            facts=facts,
            policy=request.contract.policy,
            warnings=warnings,
            total_ms=elapsed_ms,
        )
        validation_start = time.perf_counter_ns()
        metadata = validate_snapshot(
            attempt.artifact_path,
            project_id=request.workspace.project_id,
            expected_policy_version=request.contract.policy.policy_version,
            expected_repo_root_norm=request.workspace.repo_root_norm,
            expected_repository_identity_hash=request.workspace.repository_identity_hash,
            expected_repository_binding_generation=request.workspace.binding_generation,
        )
        validation_ms = (time.perf_counter_ns() - validation_start) // 1_000_000
        capture_state = "SEALED"

        final_candidates = git_candidates(request.repository_root, request.contract.policy)
        final_state = capture_repo_state(
            request.repository_root,
            final_candidates,
            request.contract.policy,
        )
        if (
            before != final_state
            or not final_state.visibility_sealed
            or not verify_scan_evidence(
                request.repository_root,
                facts,
                request.contract.policy,
            )
        ):
            raise RepositoryChangedError(
                "repository changed after snapshot validation and before publication"
            )
        _require_self_contained_artifact(attempt.artifact_path)
        if not request.workspace_matches_repository(
            request.repository_root,
            request.workspace,
        ):
            raise RepositoryChangedError(
                "repository root, identity, or registration changed before capture seal"
            )
        timings = dict(facts.timings)
        timings["database_write_ms"] = database_write_ms
        timings["validation_ms"] = validation_ms
        return _build_descriptor(
            request=request,
            attempt=attempt,
            metadata=metadata,
            before=before,
            after=after,
            final_state=final_state,
            logical_fingerprint=logical_fingerprint,
            observation_fingerprint=observation_fingerprint,
            snapshot_size=snapshot_size,
            counts=facts.counts(),
            timings=timings,
            warnings=warnings,
            started_at=started_at,
            started_perf_ns=start_ns,
            finished_at=finished_at,
        )
    except BaseException as exc:
        exc.capture_state = capture_state  # type: ignore[attr-defined]
        exc.capture_artifact_owned = True  # type: ignore[attr-defined]
        _cleanup_capture_artifact(ownership)
        raise


def _build_descriptor(
    *,
    request: CaptureRequest,
    attempt: CaptureAttempt,
    metadata: dict[str, Any],
    before: RepoState,
    after: RepoState,
    final_state: RepoState,
    logical_fingerprint: str,
    observation_fingerprint: str,
    snapshot_size: int,
    counts: dict[str, int],
    timings: dict[str, int],
    warnings: list[dict[str, Any]],
    started_at: str,
    started_perf_ns: int,
    finished_at: str,
) -> SealedArtifactDescriptor:
    contract = request.contract
    expected = {
        "snapshot_id": attempt.snapshot_id,
        "run_id": attempt.run_id,
        "project_id": request.workspace.project_id,
        "schema_version": contract.schema_version,
        "scanner_version": contract.scanner_version,
        "policy_version": contract.policy.policy_version,
        "proof_contract_version": contract.proof_contract_version,
        "verifier_version": contract.verifier_version,
        "module_map_version": contract.module_map_version,
        "occurrence_contract_version": contract.occurrence_contract_version,
        "build_status": "SEALED",
    }
    mismatches = {
        key: {"expected": value, "actual": metadata.get(key)}
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    actual_extractors = tuple(sorted(metadata.get("extractor_versions", {}).items()))
    if actual_extractors != contract.extractor_versions:
        mismatches["extractor_versions"] = {
            "expected": contract.extractor_versions,
            "actual": actual_extractors,
        }
    actual_size = attempt.artifact_path.stat().st_size
    if actual_size != snapshot_size:
        mismatches["size_bytes"] = {"expected": snapshot_size, "actual": actual_size}
    if mismatches:
        raise IndexingError(
            "Sealed artifact descriptor evidence does not match the validated snapshot.",
            details={"descriptor_mismatches": mismatches},
        )
    return SealedArtifactDescriptor(
        artifact_path=attempt.artifact_path,
        workspace=request.workspace,
        snapshot_id=attempt.snapshot_id,
        run_id=attempt.run_id,
        created_at=finished_at,
        repository_state_before=before,
        repository_state_after=after,
        repository_state_final=final_state,
        schema_version=contract.schema_version,
        scanner_version=contract.scanner_version,
        policy_version=contract.policy.policy_version,
        extractor_versions=contract.extractor_versions,
        proof_contract_version=contract.proof_contract_version,
        verifier_version=contract.verifier_version,
        module_map_version=contract.module_map_version,
        occurrence_contract_version=contract.occurrence_contract_version,
        logical_fingerprint=logical_fingerprint,
        observation_fingerprint=observation_fingerprint,
        size_bytes=snapshot_size,
        counts=counts,
        timings=timings,
        warnings=tuple(warnings),
        started_at=started_at,
        started_perf_ns=started_perf_ns,
    )


def failure_attempt(error: BaseException) -> CaptureAttempt | None:
    attempt = getattr(error, "capture_attempt", None)
    return attempt if isinstance(attempt, CaptureAttempt) else None


def failure_artifact_owned(error: BaseException) -> bool | None:
    owned = getattr(error, "capture_artifact_owned", None)
    return owned if isinstance(owned, bool) else None


def _annotate_attempt(
    error: BaseException,
    attempt: CaptureAttempt,
    attempt_number: int,
) -> None:
    error.capture_attempt = attempt  # type: ignore[attr-defined]
    error.capture_attempt_number = attempt_number  # type: ignore[attr-defined]


def _failure_state(error: BaseException) -> str:
    state = getattr(error, "capture_state", "BUILDING")
    return state if state in {"BUILDING", "SEALED"} else "BUILDING"


def _require_current_contract(contract: CaptureContract) -> None:
    current = CaptureContract.current(contract.policy)
    if contract != current:
        raise IndexingError(
            "Capture contract does not match the installed trusted capture engine.",
            code="CAPTURE_CONTRACT_MISMATCH",
            retryable=False,
        )


def _preflight_capture_request(request: CaptureRequest) -> None:
    if not request.attempts:
        raise IndexingError(
            "Trusted capture requires at least one caller-owned attempt.",
            code="CAPTURE_REQUEST_INVALID",
            retryable=False,
            details={"reason": "attempts_missing"},
        )

    snapshot_ids: set[str] = set()
    run_ids: set[str] = set()
    artifact_paths: set[str] = set()
    owned_paths: set[str] = set()
    repository_root = _normalized_path(request.repository_root)
    for attempt in request.attempts:
        if attempt.snapshot_id in snapshot_ids:
            raise _capture_request_error("duplicate_snapshot_id", attempt)
        if attempt.run_id in run_ids:
            raise _capture_request_error("duplicate_run_id", attempt)
        snapshot_ids.add(attempt.snapshot_id)
        run_ids.add(attempt.run_id)

        if not attempt.artifact_path.is_absolute():
            raise _capture_destination_error("destination_not_absolute", attempt)
        artifact_path = _normalized_path(attempt.artifact_path)
        if artifact_path in artifact_paths:
            raise _capture_destination_error("duplicate_artifact_path", attempt)
        artifact_paths.add(artifact_path)
        if _path_is_within(artifact_path, repository_root):
            raise _capture_destination_error("destination_inside_repository", attempt)

        for candidate in _artifact_paths(attempt.artifact_path):
            owned_path = _normalized_path(candidate)
            if owned_path in owned_paths:
                raise _capture_destination_error("attempt_paths_overlap", attempt, candidate)
            owned_paths.add(owned_path)
            if _path_lexists(candidate):
                raise _capture_destination_error("destination_not_fresh", attempt, candidate)


def _capture_request_error(reason: str, attempt: CaptureAttempt) -> IndexingError:
    return IndexingError(
        "Trusted capture attempt identities must be unique.",
        code="CAPTURE_REQUEST_INVALID",
        retryable=False,
        details={
            "reason": reason,
            "snapshot_id": attempt.snapshot_id,
            "run_id": attempt.run_id,
        },
    )


def _capture_destination_error(
    reason: str,
    attempt: CaptureAttempt,
    path: Path | None = None,
    *,
    error: OSError | None = None,
    owned: bool = False,
) -> IndexingError:
    details: dict[str, Any] = {
        "reason": reason,
        "artifact_path": str(attempt.artifact_path),
        "path": str(path or attempt.artifact_path),
    }
    if error is not None:
        details["error_type"] = type(error).__name__
        details["winerror"] = getattr(error, "winerror", None)
    capture_error = IndexingError(
        "Trusted capture requires a fresh caller-owned artifact destination.",
        code="CAPTURE_DESTINATION_INVALID",
        retryable=False,
        details=details,
    )
    capture_error.capture_artifact_owned = owned  # type: ignore[attr-defined]
    return capture_error


def _claim_capture_artifact(attempt: CaptureAttempt) -> _CaptureArtifactOwnership:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(attempt.artifact_path, flags, 0o600)
    except OSError as exc:
        raise _capture_destination_error(
            "destination_claim_failed",
            attempt,
            error=exc,
        ) from exc
    try:
        os.close(descriptor)
    except OSError as exc:
        _cleanup_owned_paths((attempt.artifact_path,))
        raise _capture_destination_error(
            "destination_claim_close_failed",
            attempt,
            error=exc,
            owned=True,
        ) from exc

    sidecars = _artifact_paths(attempt.artifact_path)[1:]
    existing_sidecars = [candidate for candidate in sidecars if _path_lexists(candidate)]
    if existing_sidecars:
        _cleanup_owned_paths((attempt.artifact_path,))
        raise _capture_destination_error(
            "sidecar_appeared_before_attempt",
            attempt,
            existing_sidecars[0],
            owned=True,
        )
    return _CaptureArtifactOwnership(paths=(attempt.artifact_path, *sidecars))


def _require_self_contained_artifact(path: Path) -> None:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise IndexingError("Trusted capture did not produce a regular sealed artifact.")
    sidecars = [candidate for candidate in _artifact_paths(path)[1:] if _path_lexists(candidate)]
    if sidecars:
        raise IndexingError(
            "Trusted capture left required SQLite sidecar files.",
            details={"sidecars": [str(candidate) for candidate in sidecars]},
        )


def _cleanup_capture_artifact(ownership: _CaptureArtifactOwnership) -> None:
    _cleanup_owned_paths(ownership.paths)


def _cleanup_owned_paths(paths: tuple[Path, ...]) -> None:
    for candidate in reversed(paths):
        try:
            if (
                _path_lexists(candidate)
                and not candidate.is_symlink()
                and not candidate.is_junction()
            ):
                candidate.unlink()
        except OSError:
            pass


def _artifact_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path.expanduser().resolve(strict=False)))


def _path_is_within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath((path, directory)) == directory
    except ValueError:
        return False


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _warnings(facts: Any) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if facts.diagnostics:
        warnings.append(
            {
                "code": "PYTHON_PARSE_FAILURES",
                "message": (
                    "Some Python files could not be parsed; current metadata and "
                    "diagnostics were stored."
                ),
                "details": {"count": len(facts.diagnostics)},
            }
        )
    skipped = [
        item
        for item in facts.files
        if item.analysis_level == "METADATA_ONLY" and item.file_kind not in {"HARD_SECRET"}
    ]
    if skipped:
        warnings.append(
            {
                "code": "METADATA_ONLY_FILES",
                "message": "Some candidates were stored as metadata only.",
                "details": {"count": len(skipped)},
            }
        )
    return warnings


def _precise_utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
