"""Structural index build orchestration and safe publication."""

import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project_kb.errors import IndexingError, ProjectKbError, ProjectStatusError
from project_kb.gating import GateContext, GateRequirement, evaluate_gate
from project_kb.indexing.models import IndexOutcome, ScanPolicy
from project_kb.indexing.scanner import (
    RepositoryChangedError,
    ScanError,
    capture_repo_state,
    git_candidates,
    scan_repository,
    verify_scan_evidence,
)
from project_kb.registry.service import RegistryService, utc_now
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.state import ProjectState
from project_kb.snapshot.database import (
    EXPECTED_EXTRACTORS,
    SCANNER_VERSION,
    SCHEMA_VERSION,
    publish_snapshot,
    validate_snapshot,
    write_snapshot,
)


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
        repo_root = Path(project.repo_root)
        storage_path = Path(project.storage_path)
        current_path = storage_path / "kb.sqlite"
        previous_snapshot = current_path.exists()
        last_error: ProjectKbError | None = None
        for attempt in range(2):
            run_id = uuid.uuid4().hex
            snapshot_id = uuid.uuid4().hex
            temp_path = storage_path / "runs" / f"kb-{run_id}.tmp.sqlite"
            start_ns = time.perf_counter_ns()
            publication_state = "BUILDING"
            bookkeeping = {"registry": "unknown", "run_file": "unknown"}
            started_at = _precise_utc_now()
            try:
                enumeration_start = time.perf_counter_ns()
                candidates = git_candidates(repo_root, self.policy)
                before = capture_repo_state(repo_root, candidates, self.policy)
                enumeration_ms = (time.perf_counter_ns() - enumeration_start) // 1_000_000
                facts = scan_repository(repo_root, candidates, self.policy)
                facts.timings["enumeration_ms"] = enumeration_ms
                evidence_stable = verify_scan_evidence(repo_root, facts, self.policy)
                after_candidates = git_candidates(repo_root, self.policy)
                after = capture_repo_state(repo_root, after_candidates, self.policy)
                if before != after or not evidence_stable:
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
                    temp_path,
                    snapshot_id=snapshot_id,
                    run_id=run_id,
                    project_id=project.project_id,
                    repo_root_norm=project.repo_root_norm,
                    created_at=finished_at,
                    started_at=started_at,
                    finished_at=finished_at,
                    before=before,
                    after=after,
                    facts=facts,
                    policy=self.policy,
                    warnings=warnings,
                    total_ms=elapsed_ms,
                )
                validation_start = time.perf_counter_ns()
                validate_snapshot(
                    temp_path,
                    project_id=project.project_id,
                    expected_policy_version=self.policy.policy_version,
                )
                validation_ms = (time.perf_counter_ns() - validation_start) // 1_000_000
                publication_state = "SEALED"
                final_candidates = git_candidates(repo_root, self.policy)
                final_state = capture_repo_state(repo_root, final_candidates, self.policy)
                if before != final_state or not verify_scan_evidence(repo_root, facts, self.policy):
                    raise RepositoryChangedError(
                        "repository changed after snapshot validation and before publication"
                    )
                publication_start = time.perf_counter_ns()
                preserved = publish_snapshot(temp_path, current_path)
                publication_ms = (time.perf_counter_ns() - publication_start) // 1_000_000
                publication_state = "PUBLISHED"
                total_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
                try:
                    self.registry.record_index_outcome(
                        project.project_id,
                        status="INDEX_SUCCEEDED",
                        indexed_at=finished_at,
                        git_commit=before.head,
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
                counts = facts.counts()
                timings = dict(facts.timings)
                timings["database_write_ms"] = database_write_ms
                timings["validation_ms"] = validation_ms
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
                            "details": {
                                "bookkeeping_status": bookkeeping["run_file"],
                            },
                        }
                    )
                if all(value == "recorded" for value in bookkeeping.values()):
                    publication_state = "POST_PUBLICATION_RECORDED"
                return IndexOutcome(
                    result="success_with_warnings" if warnings else "success",
                    code="INDEX_PUBLISHED_WITH_WARNINGS" if warnings else "INDEX_PUBLISHED",
                    message="Structural snapshot published with warnings."
                    if warnings
                    else "Structural snapshot published.",
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
                self._safe_cleanup(temp_path)
                self._record_run_file(
                    storage_path,
                    run_id,
                    status="FAILED",
                    failure_code="REPO_CHANGED_DURING_SCAN",
                    details={
                        "attempt": attempt + 1,
                        "publication_state": publication_state,
                        "message": str(exc),
                    },
                )
                if attempt == 0:
                    continue
                last_error = IndexingError(
                    "Repository changed during both bounded scan attempts.",
                    code="REPO_CHANGED_DURING_SCAN",
                    details={
                        "attempts": 2,
                        "previous_snapshot_preserved": previous_snapshot,
                        "publication_state": publication_state,
                    },
                )
                self._record_failure(project.project_id, storage_path, run_id, last_error)
                break
            except ProjectKbError as exc:
                if exc.details.get("published") is True or (
                    publication_state == "SEALED"
                    and self._canonical_run_is_current(
                        current_path,
                        project_id=project.project_id,
                        run_id=run_id,
                    )
                ):
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
                    )
                exc.details.setdefault("publication_state", publication_state)
                last_error = exc
                self._safe_cleanup(temp_path)
                self._record_failure(project.project_id, storage_path, run_id, exc)
                break
            except (OSError, ScanError, ValueError) as exc:
                last_error = IndexingError(
                    "Structural index build failed before publication.",
                    details={
                        "error_type": type(exc).__name__,
                        "previous_snapshot_preserved": previous_snapshot,
                        "publication_state": publication_state,
                    },
                )
                self._safe_cleanup(temp_path)
                self._record_failure(project.project_id, storage_path, run_id, last_error)
                break
            except Exception as exc:
                if publication_state == "SEALED" and self._canonical_run_is_current(
                    current_path,
                    project_id=project.project_id,
                    run_id=run_id,
                ):
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
                    )
                last_error = IndexingError(
                    "Structural index build failed unexpectedly before safe completion.",
                    details={
                        "error_type": type(exc).__name__,
                        "previous_snapshot_preserved": current_path.exists(),
                        "publication_state": publication_state,
                    },
                )
                self._safe_cleanup(temp_path)
                self._record_failure(project.project_id, storage_path, run_id, last_error)
                break
        assert last_error is not None
        raise last_error

    def _canonical_run_is_current(
        self,
        path: Path,
        *,
        project_id: str,
        run_id: str,
    ) -> bool:
        try:
            meta = validate_snapshot(
                path,
                project_id=project_id,
                expected_policy_version=self.policy.policy_version,
            )
        except ProjectKbError:
            return False
        return meta.get("run_id") == run_id and meta.get("build_status") == "SEALED"

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
    ) -> IndexOutcome:
        warning = {
            "code": "POST_PUBLICATION_ANCILLARY_FAILURE",
            "message": (
                "The snapshot is current, but later ancillary processing did not complete."
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
                "snapshot": {"snapshot_id": snapshot_id, "published": True},
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
    def _safe_cleanup(path: Path) -> None:
        try:
            if path.exists() and not path.is_symlink() and not path.is_junction():
                path.unlink()
        except OSError:
            pass


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
