"""Structural index build orchestration and safe publication."""

import contextlib
import json
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from project_kb.errors import IndexingError, ProjectKbError, ProjectStatusError
from project_kb.gating import GateContext, GateRequirement, evaluate_gate
from project_kb.indexing import capture as capture_module
from project_kb.indexing.capture import (
    CaptureAttempt,
    CaptureContract,
    CaptureRequest,
    CaptureRetryEvent,
    CaptureWorkspace,
)
from project_kb.indexing.models import IndexOutcome, ScanPolicy
from project_kb.indexing.scanner import RepositoryChangedError, ScanError
from project_kb.registry.service import RegistryService, utc_now
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.repo_identity import (
    RepositoryFingerprint,
    build_repository_fingerprint,
    compare_repository_fingerprints,
    normalize_path,
    repository_identity_hash,
)
from project_kb.resolver.state import ProjectState
from project_kb.snapshot.database import (
    EXPECTED_EXTRACTORS,
    SCANNER_VERSION,
    SCHEMA_VERSION,
    publish_snapshot,
    validate_snapshot,
)


class _CanonicalPublicationState(StrEnum):
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    NOT_PUBLISHED = "NOT_PUBLISHED"


class IndexService:
    def __init__(
        self,
        *,
        home: Path | None = None,
        working_directory: Path | None = None,
    ) -> None:
        self.status_service = ProjectStatusService(home=home, working_directory=working_directory)
        self.registry = RegistryService(home=self.status_service.home)
        self.policy = ScanPolicy()

    def index(
        self,
        project_name: str | None = None,
        *,
        requested_mode: str = "default",
    ) -> IndexOutcome:
        status = self.status_service.status(project_name)
        snapshot_rebuild_allowed = status.project_state in {
            ProjectState.SNAPSHOT_STORAGE_ERROR,
            ProjectState.SNAPSHOT_REBUILD_REQUIRED,
        }
        if status.project is None or (
            not status.availability.can_use_project and not snapshot_rebuild_allowed
        ):
            if status.error:
                raise status.error
            raise ProjectStatusError(
                code=status.code,
                message=status.message,
                exit_code=status.exit_code,
                recommended_action=status.recommended_action.code,
                details={"project_state": status.project_state.value},
            )
        gate = evaluate_gate(
            GateContext(
                registry_available=True,
                project_resolved=True,
                repo_valid=True,
                storage_valid=True,
            ),
            (
                GateRequirement.REGISTRY_AVAILABLE,
                GateRequirement.PROJECT_RESOLVED,
                GateRequirement.REPO_VALID,
                GateRequirement.STORAGE_VALID,
            ),
        )
        if not gate.allowed:
            raise IndexingError("Index command gating failed.", retryable=False)

        project = status.project
        active_repository_identity_hash = repository_identity_hash(
            project.repo_root_norm,
            project.repo_fingerprint_json,
        )
        repo_root = Path(project.repo_root)
        storage_path = Path(project.storage_path)
        current_path = storage_path / "kb.sqlite"
        previous_snapshot = current_path.exists()
        attempts = tuple(
            CaptureAttempt(
                artifact_path=storage_path / "runs" / f"kb-{run_id}.tmp.sqlite",
                snapshot_id=uuid.uuid4().hex,
                run_id=run_id,
            )
            for run_id in (uuid.uuid4().hex for _ in range(2))
        )
        active_attempt = attempts[0]
        run_id = active_attempt.run_id
        snapshot_id = active_attempt.snapshot_id
        temp_path = active_attempt.artifact_path
        publication_state = "BUILDING"
        bookkeeping = {"registry": "unknown", "run_file": "unknown"}
        try:
            workspace = CaptureWorkspace(
                project_id=project.project_id,
                workspace_id=project.workspace_id,
                repo_root_norm=project.repo_root_norm,
                repository_identity_hash=active_repository_identity_hash,
                binding_generation=project.repo_binding_generation,
            )
            descriptor = capture_module.capture_trusted_artifact(
                CaptureRequest(
                    workspace=workspace,
                    repository_root=repo_root,
                    attempts=attempts,
                    contract=CaptureContract.current(self.policy),
                    workspace_matches_repository=lambda actual_root, expected: (
                        self._active_binding_matches(
                            project.project_name,
                            actual_root,
                            expected,
                        )
                    ),
                ),
                retry_observer=lambda event: self._record_capture_retry(storage_path, event),
            )
            active_attempt = CaptureAttempt(
                artifact_path=descriptor.artifact_path,
                snapshot_id=descriptor.snapshot_id,
                run_id=descriptor.run_id,
            )
            run_id = active_attempt.run_id
            snapshot_id = active_attempt.snapshot_id
            temp_path = active_attempt.artifact_path
            publication_state = "SEALED"
            before = descriptor.repository_state_before
            warnings = list(descriptor.warnings)
            finished_at = descriptor.created_at
            logical_fingerprint = descriptor.logical_fingerprint
            observation_fingerprint = descriptor.observation_fingerprint
            snapshot_size = descriptor.size_bytes
            counts = dict(descriptor.counts)
            timings = dict(descriptor.timings)
            publication_start = time.perf_counter_ns()
            preserved = publish_snapshot(descriptor.artifact_path, current_path)
            publication_ms = (time.perf_counter_ns() - publication_start) // 1_000_000
            publication_state = "PUBLISHED"
            total_ms = (time.perf_counter_ns() - descriptor.started_perf_ns) // 1_000_000
            try:
                self.registry.record_index_outcome(
                    project.project_id,
                    status="INDEX_SUCCEEDED",
                    indexed_at=finished_at,
                    git_commit=before.head,
                    binding_generation=project.repo_binding_generation,
                )
                bookkeeping["registry"] = "recorded"
            except ProjectKbError as exc:
                bookkeeping["registry"] = "not_recorded"
                warnings.append(
                    {
                        "code": "REGISTRY_INDEX_STATUS_NOT_RECORDED",
                        "message": (
                            "Snapshot was published, but the registry outcome metadata "
                            "could not be updated."
                        ),
                        "details": {
                            "error_type": type(exc).__name__,
                            "bookkeeping_status": bookkeeping["registry"],
                        },
                    }
                )
            except Exception as exc:
                bookkeeping["registry"] = "unknown"
                warnings.append(
                    {
                        "code": "REGISTRY_INDEX_STATUS_COMPLETION_UNKNOWN",
                        "message": (
                            "Snapshot was published, but registry bookkeeping "
                            "completion is uncertain."
                        ),
                        "details": {
                            "error_type": type(exc).__name__,
                            "bookkeeping_status": bookkeeping["registry"],
                        },
                    }
                )
            timings["publication_ms"] = publication_ms
            timings["total_ms"] = total_ms
            try:
                run_recorded = self._record_run_file(
                    storage_path,
                    run_id,
                    status="SUCCESS",
                    details={
                        "snapshot_id": snapshot_id,
                        "publication_state": publication_state,
                        "counts": counts,
                        "timings": timings,
                        "snapshot_size_bytes": snapshot_size,
                    },
                )
                bookkeeping["run_file"] = "recorded" if run_recorded else "not_recorded"
            except OSError:
                bookkeeping["run_file"] = "not_recorded"
            except Exception:
                bookkeeping["run_file"] = "unknown"
            if bookkeeping["run_file"] != "recorded":
                warnings.append(
                    {
                        "code": "POST_PUBLICATION_RUN_RECORD_NOT_WRITTEN",
                        "message": (
                            "Snapshot was published, but its ancillary run record "
                            "was not confirmed."
                        ),
                        "details": {"bookkeeping_status": bookkeeping["run_file"]},
                    }
                )
            if all(value == "recorded" for value in bookkeeping.values()):
                publication_state = "POST_PUBLICATION_RECORDED"
            canonical_state = self._canonical_publication_state(
                current_path,
                project_id=project.project_id,
                project_name=project.project_name,
                run_id=run_id,
                snapshot_id=snapshot_id,
                repo_root_norm=project.repo_root_norm,
                expected_repository_identity_hash=active_repository_identity_hash,
                repository_binding_generation=project.repo_binding_generation,
            )
            if canonical_state is not _CanonicalPublicationState.ACTIVE:
                raise self._publication_state_error(canonical_state)
            return IndexOutcome(
                result="success_with_warnings" if warnings else "success",
                code="INDEX_PUBLISHED_WITH_WARNINGS" if warnings else "INDEX_PUBLISHED",
                message=(
                    "Structural snapshot published with warnings."
                    if warnings
                    else "Structural snapshot published."
                ),
                data={
                    "resolution": status.resolution.to_dict(),
                    "project": project.to_dict(),
                    "index_run": {
                        "run_id": run_id,
                        "requested_mode": requested_mode,
                        "effective_mode": "full",
                        "status": "success",
                    },
                    "snapshot": {
                        "snapshot_id": snapshot_id,
                        "published": True,
                        "truth_claim": "CAPTURED_STABLE",
                        "schema_version": SCHEMA_VERSION,
                        "scanner_version": SCANNER_VERSION,
                        "policy_version": self.policy.policy_version,
                        "extractor_versions": EXPECTED_EXTRACTORS,
                        "logical_fingerprint": logical_fingerprint,
                        "observation_fingerprint": observation_fingerprint,
                        "size_bytes": snapshot_size,
                    },
                    "counts": counts,
                    "timings": timings,
                    "previous_snapshot": {"preserved": False, "replaced": preserved},
                    "publication": {
                        "atomic": True,
                        "state": publication_state,
                        "bookkeeping": dict(bookkeeping),
                    },
                },
                warnings=warnings,
            )
        except RepositoryChangedError as exc:
            active_attempt = capture_module.failure_attempt(exc) or active_attempt
            run_id = active_attempt.run_id
            temp_path = active_attempt.artifact_path
            publication_state = _capture_state(exc, publication_state)
            self._safe_cleanup_capture_error(temp_path, exc)
            last_error = IndexingError(
                "Repository changed during both bounded scan attempts.",
                code="REPO_CHANGED_DURING_SCAN",
                details={
                    "attempts": len(attempts),
                    "previous_snapshot_preserved": previous_snapshot,
                    "publication_state": publication_state,
                },
            )
            self._record_failure(project.project_id, storage_path, run_id, last_error)
            raise last_error from exc
        except ProjectKbError as exc:
            active_attempt = capture_module.failure_attempt(exc) or active_attempt
            run_id = active_attempt.run_id
            snapshot_id = active_attempt.snapshot_id
            temp_path = active_attempt.artifact_path
            publication_state = _capture_state(exc, publication_state)
            if exc.details.get("usable") is False:
                self._safe_cleanup_capture_error(temp_path, exc)
                raise
            if publication_state == "SEALED":
                recovered_state = self._canonical_publication_state(
                    current_path,
                    project_id=project.project_id,
                    project_name=project.project_name,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    repo_root_norm=project.repo_root_norm,
                    expected_repository_identity_hash=active_repository_identity_hash,
                    repository_binding_generation=project.repo_binding_generation,
                )
                if recovered_state is _CanonicalPublicationState.QUARANTINED:
                    self._safe_cleanup_capture_error(temp_path, exc)
                    raise self._publication_state_error(recovered_state) from exc
                if recovered_state is _CanonicalPublicationState.ACTIVE:
                    publication_state = "PUBLISHED"
            if exc.details.get("published") is True:
                publication_state = "PUBLISHED"
            if publication_state in {"PUBLISHED", "POST_PUBLICATION_RECORDED"}:
                return self._published_fallback(
                    status=status,
                    project=project,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    requested_mode=requested_mode,
                    publication_state=publication_state,
                    bookkeeping=bookkeeping,
                    error=exc,
                    current_path=current_path,
                    repository_identity_hash=active_repository_identity_hash,
                    repository_binding_generation=project.repo_binding_generation,
                )
            exc.details.setdefault("publication_state", publication_state)
            self._safe_cleanup_capture_error(temp_path, exc)
            self._record_failure(project.project_id, storage_path, run_id, exc)
            raise
        except (OSError, ScanError, ValueError) as exc:
            active_attempt = capture_module.failure_attempt(exc) or active_attempt
            run_id = active_attempt.run_id
            temp_path = active_attempt.artifact_path
            publication_state = _capture_state(exc, publication_state)
            last_error = IndexingError(
                "Structural index build failed before publication.",
                details={
                    "error_type": type(exc).__name__,
                    "previous_snapshot_preserved": previous_snapshot,
                    "publication_state": publication_state,
                },
            )
            self._safe_cleanup_capture_error(temp_path, exc)
            self._record_failure(project.project_id, storage_path, run_id, last_error)
            raise last_error from exc
        except Exception as exc:
            active_attempt = capture_module.failure_attempt(exc) or active_attempt
            run_id = active_attempt.run_id
            snapshot_id = active_attempt.snapshot_id
            temp_path = active_attempt.artifact_path
            publication_state = _capture_state(exc, publication_state)
            if publication_state == "SEALED":
                recovered_state = self._canonical_publication_state(
                    current_path,
                    project_id=project.project_id,
                    project_name=project.project_name,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    repo_root_norm=project.repo_root_norm,
                    expected_repository_identity_hash=active_repository_identity_hash,
                    repository_binding_generation=project.repo_binding_generation,
                )
                if recovered_state is _CanonicalPublicationState.ACTIVE:
                    publication_state = "PUBLISHED"
                elif recovered_state is _CanonicalPublicationState.QUARANTINED:
                    raise self._publication_state_error(recovered_state) from exc
            if publication_state in {"PUBLISHED", "POST_PUBLICATION_RECORDED"}:
                return self._published_fallback(
                    status=status,
                    project=project,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    requested_mode=requested_mode,
                    publication_state=publication_state,
                    bookkeeping=bookkeeping,
                    error=exc,
                    current_path=current_path,
                    repository_identity_hash=active_repository_identity_hash,
                    repository_binding_generation=project.repo_binding_generation,
                )
            last_error = IndexingError(
                "Structural index build failed unexpectedly before safe completion.",
                details={
                    "error_type": type(exc).__name__,
                    "previous_snapshot_preserved": current_path.exists(),
                    "publication_state": publication_state,
                },
            )
            self._safe_cleanup_capture_error(temp_path, exc)
            self._record_failure(project.project_id, storage_path, run_id, last_error)
            raise last_error from exc

    def _active_binding_matches(
        self,
        project_name: str,
        repository_root: Path,
        expected: CaptureWorkspace,
    ) -> bool:
        active_project = self.registry.find_project_by_name(project_name)
        if active_project is None or active_project.repo_fingerprint_json is None:
            return False
        try:
            actual_root_norm = normalize_path(repository_root)
            expected_root_norm = normalize_path(Path(expected.repo_root_norm))
            registered_root_norm = normalize_path(Path(active_project.repo_root))
            stored_fingerprint = RepositoryFingerprint.from_json(
                active_project.repo_fingerprint_json
            )
            live_fingerprint = build_repository_fingerprint(repository_root)
            fingerprint_matches = compare_repository_fingerprints(
                stored_fingerprint,
                live_fingerprint,
                repo_root=repository_root,
            ).matches
        except OSError, ValueError:
            return False
        active_identity_hash = repository_identity_hash(
            active_project.repo_root_norm,
            active_project.repo_fingerprint_json,
        )
        return (
            active_project.project_id == expected.project_id
            and active_project.workspace_id == expected.workspace_id
            and active_project.workspace_state == "ACTIVE"
            and actual_root_norm == expected_root_norm
            and actual_root_norm == registered_root_norm
            and active_project.repo_root_norm == expected.repo_root_norm
            and active_identity_hash == expected.repository_identity_hash
            and active_project.repo_binding_generation == expected.binding_generation
            and fingerprint_matches
        )

    def _record_capture_retry(
        self,
        storage_path: Path,
        event: CaptureRetryEvent,
    ) -> None:
        self._record_run_file(
            storage_path,
            event.attempt.run_id,
            status="FAILED",
            failure_code="REPO_CHANGED_DURING_SCAN",
            details={
                "attempt": event.attempt_number,
                "publication_state": event.publication_state,
                "message": str(event.error),
            },
        )

    def _canonical_publication_state(
        self,
        path: Path,
        *,
        project_id: str,
        project_name: str,
        run_id: str,
        snapshot_id: str,
        repo_root_norm: str,
        expected_repository_identity_hash: str,
        repository_binding_generation: str,
    ) -> _CanonicalPublicationState:
        try:
            meta = validate_snapshot(
                path,
                project_id=project_id,
                expected_policy_version=self.policy.policy_version,
                expected_repo_root_norm=repo_root_norm,
                expected_repository_identity_hash=expected_repository_identity_hash,
                expected_repository_binding_generation=repository_binding_generation,
            )
        except ProjectKbError:
            return _CanonicalPublicationState.NOT_PUBLISHED
        if not (
            meta.get("schema_version") == SCHEMA_VERSION
            and meta.get("snapshot_id") == snapshot_id
            and meta.get("run_id") == run_id
            and meta.get("build_status") == "SEALED"
        ):
            return _CanonicalPublicationState.NOT_PUBLISHED
        active_project = self.registry.find_project_by_name(project_name)
        if active_project is None:
            return _CanonicalPublicationState.QUARANTINED
        active_identity_hash = repository_identity_hash(
            active_project.repo_root_norm,
            active_project.repo_fingerprint_json,
        )
        if (
            active_project.project_id != project_id
            or active_project.repo_root_norm != repo_root_norm
            or active_identity_hash != expected_repository_identity_hash
            or active_project.repo_binding_generation != repository_binding_generation
        ):
            return _CanonicalPublicationState.QUARANTINED
        return _CanonicalPublicationState.ACTIVE

    @staticmethod
    def _publication_state_error(state: _CanonicalPublicationState) -> IndexingError:
        publication_state = (
            "PUBLISHED_QUARANTINED"
            if state is _CanonicalPublicationState.QUARANTINED
            else "PUBLICATION_NOT_CONFIRMED"
        )
        return IndexingError(
            (
                "The snapshot was published physically but is not active for the "
                "current repository binding."
            )
            if state is _CanonicalPublicationState.QUARANTINED
            else "The canonical snapshot publication could not be confirmed safely.",
            code="SNAPSHOT_REBUILD_REQUIRED"
            if state is _CanonicalPublicationState.QUARANTINED
            else "INDEX_PUBLICATION_NOT_CONFIRMED",
            details={
                "published": state is _CanonicalPublicationState.QUARANTINED,
                "usable": False,
                "publication_state": publication_state,
            },
        )

    def _published_fallback(
        self,
        *,
        status: Any,
        project: Any,
        run_id: str,
        snapshot_id: str,
        requested_mode: str,
        publication_state: str,
        bookkeeping: dict[str, str],
        error: Exception,
        current_path: Path,
        repository_identity_hash: str,
        repository_binding_generation: str,
    ) -> IndexOutcome:
        canonical_state = self._canonical_publication_state(
            current_path,
            project_id=project.project_id,
            project_name=project.project_name,
            run_id=run_id,
            snapshot_id=snapshot_id,
            repo_root_norm=project.repo_root_norm,
            expected_repository_identity_hash=repository_identity_hash,
            repository_binding_generation=repository_binding_generation,
        )
        if canonical_state is not _CanonicalPublicationState.ACTIVE:
            raise self._publication_state_error(canonical_state)
        warning = {
            "code": "POST_PUBLICATION_ANCILLARY_FAILURE",
            "message": (
                "The snapshot was published successfully, but later ancillary "
                "processing did not complete."
            ),
            "details": {
                "error_type": type(error).__name__,
                "publication_state": publication_state,
                "bookkeeping": dict(bookkeeping),
            },
        }
        return IndexOutcome(
            result="success_with_warnings",
            code="INDEX_PUBLISHED_WITH_WARNINGS",
            message="Structural snapshot published with warnings.",
            data={
                "resolution": status.resolution.to_dict(),
                "project": project.to_dict(),
                "index_run": {
                    "run_id": run_id,
                    "requested_mode": requested_mode,
                    "effective_mode": "full",
                    "status": "success",
                },
                "snapshot": {
                    "snapshot_id": snapshot_id,
                    "published": True,
                    "truth_claim": "CAPTURED_STABLE",
                },
                "counts": {},
                "timings": {},
                "previous_snapshot": {"preserved": False},
                "publication": {
                    "atomic": True,
                    "state": publication_state,
                    "bookkeeping": dict(bookkeeping),
                },
            },
            warnings=[warning],
        )

    def _record_failure(
        self,
        project_id: str,
        storage_path: Path,
        run_id: str,
        error: ProjectKbError,
    ) -> None:
        with contextlib.suppress(Exception):
            self._record_run_file(
                storage_path,
                run_id,
                status="FAILED",
                failure_code=error.code,
                details={"message": error.message},
            )
        with contextlib.suppress(ProjectKbError):
            self.registry.record_index_outcome(
                project_id,
                status=f"INDEX_FAILED:{error.code}",
                failure_code=error.code,
            )

    @staticmethod
    def _record_run_file(
        storage_path: Path,
        run_id: str,
        *,
        status: str,
        details: dict[str, Any],
        failure_code: str | None = None,
    ) -> bool:
        run_path = storage_path / "runs" / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "status": status,
            "failure_code": failure_code,
            "recorded_at": utc_now(),
            "details": details,
        }
        try:
            run_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            return False
        return True

    @staticmethod
    def _safe_cleanup_capture_error(path: Path, error: BaseException) -> None:
        if capture_module.failure_artifact_owned(error) is False:
            return
        IndexService._safe_cleanup(path)

    @staticmethod
    def _safe_cleanup(path: Path) -> None:
        try:
            if path.exists() and not path.is_symlink() and not path.is_junction():
                path.unlink()
        except OSError:
            pass


def _capture_state(error: BaseException, default: str) -> str:
    state = getattr(error, "capture_state", default)
    return state if state in {"BUILDING", "SEALED"} else default
