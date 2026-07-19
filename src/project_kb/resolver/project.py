"""Project status resolution orchestration."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from project_kb.errors import (
    InvalidProjectNameError,
    ProjectKbError,
    ProjectStatusError,
    RegistryOperationError,
    SnapshotQueryError,
)
from project_kb.exit_codes import GIT_REPO_ERROR
from project_kb.gating import GateContext, GateRequirement, evaluate_gate
from project_kb.git_utils import resolve_git_root
from project_kb.registry import ProjectRecord, RegistryService
from project_kb.resolver.models import (
    Availability,
    RepoCheck,
    Resolution,
    SnapshotCheck,
    StatusOutcome,
    StorageCheck,
)
from project_kb.resolver.repo_check import check_registered_repository
from project_kb.resolver.repo_identity import (
    repository_identity_hash,
)
from project_kb.resolver.state import (
    STATE_POLICIES,
    ProjectState,
    recommended_action_for,
)
from project_kb.resolver.storage_check import check_project_storage
from project_kb.snapshot.currentness import (
    CurrentnessState,
    RepositoryBindingObservation,
    VerificationMode,
    observe_repository_binding,
    verify_snapshot_currentness,
)
from project_kb.snapshot.database import SCHEMA_VERSION, validate_snapshot
from project_kb.storage.home import resolve_home


class ProjectStatusService:
    """Resolve one project and produce the complete Stage 3 status contract."""

    def __init__(self, *, home: Path | None = None, working_directory: Path | None = None) -> None:
        self.home = home or resolve_home()
        self.working_directory = (working_directory or Path.cwd()).resolve()
        self.registry = RegistryService(home=self.home)

    def status(
        self,
        project_name: str | None = None,
        *,
        verification_mode: VerificationMode | str | None = None,
    ) -> StatusOutcome:
        mode = VerificationMode(verification_mode) if verification_mode is not None else None
        if mode is VerificationMode.FAST:
            return self._fast_verification_removed(project_name)
        if project_name is not None:
            return self._status_by_name(project_name, mode)
        return self._status_by_current_directory(mode)

    def _status_by_name(
        self,
        project_name: str,
        verification_mode: VerificationMode | None,
    ) -> StatusOutcome:
        resolution = Resolution(
            mode="name",
            status="failed",
            project_name_input=project_name,
            working_directory=str(self.working_directory),
            resolved_repo_root=None,
            resolved_by=None,
        )
        try:
            project = self.registry.find_project_by_name(project_name)
        except InvalidProjectNameError as error:
            return self._problem(
                ProjectState.UNREGISTERED,
                resolution=resolution,
                code=error.code,
                message=error.message,
                exit_code=error.exit_code,
                error_details=error.details,
            )
        except RegistryOperationError as error:
            return self._registry_problem(resolution, error)

        if project is None:
            return self._problem(
                ProjectState.UNREGISTERED,
                resolution=Resolution(
                    mode="name",
                    status="unregistered",
                    project_name_input=project_name,
                    working_directory=str(self.working_directory),
                    resolved_repo_root=None,
                    resolved_by=None,
                ),
            )

        return self._status_resolved_project(
            project,
            Resolution(
                mode="name",
                status="resolved",
                project_name_input=project_name,
                working_directory=str(self.working_directory),
                resolved_repo_root=project.repo_root,
                resolved_by="project_name",
            ),
            verification_mode,
        )

    def _status_by_current_directory(
        self,
        verification_mode: VerificationMode | None,
    ) -> StatusOutcome:
        try:
            git_root = resolve_git_root(self.working_directory)
        except ProjectKbError:
            resolution = Resolution(
                mode="current_directory",
                status="not_git_repository",
                project_name_input=None,
                working_directory=str(self.working_directory),
                resolved_repo_root=None,
                resolved_by=None,
            )
            return self._problem(
                ProjectState.UNREGISTERED,
                resolution=resolution,
                repo_check=RepoCheck(
                    status="invalid",
                    stored_repo_root=None,
                    current_git_root=None,
                    path_exists=self.working_directory.exists(),
                    is_directory=self.working_directory.is_dir(),
                    is_git_repository=False,
                    git_root_matches=None,
                    fingerprint_status="not_checked",
                    fingerprint_strength=None,
                    reason="current_directory_not_git_repository",
                ),
                code="CURRENT_DIRECTORY_NOT_GIT_REPO",
                message="Current directory is not inside a Git repository.",
                exit_code=GIT_REPO_ERROR,
            )

        resolution = Resolution(
            mode="current_directory",
            status="failed",
            project_name_input=None,
            working_directory=str(self.working_directory),
            resolved_repo_root=str(git_root),
            resolved_by=None,
        )
        try:
            project = self.registry.find_project_by_repo_root(git_root)
        except RegistryOperationError as error:
            return self._registry_problem(resolution, error)

        if project is None:
            return self._problem(
                ProjectState.UNREGISTERED,
                resolution=Resolution(
                    mode="current_directory",
                    status="unregistered",
                    project_name_input=None,
                    working_directory=str(self.working_directory),
                    resolved_repo_root=str(git_root),
                    resolved_by=None,
                ),
                repo_check=RepoCheck(
                    status="not_checked",
                    stored_repo_root=None,
                    current_git_root=str(git_root),
                    path_exists=True,
                    is_directory=True,
                    is_git_repository=True,
                    git_root_matches=None,
                    fingerprint_status="not_checked",
                    fingerprint_strength=None,
                    reason="git_repository_not_registered",
                ),
            )

        return self._status_resolved_project(
            project,
            Resolution(
                mode="current_directory",
                status="resolved",
                project_name_input=None,
                working_directory=str(self.working_directory),
                resolved_repo_root=str(git_root),
                resolved_by="repo_root",
            ),
            verification_mode,
        )

    def _status_resolved_project(
        self,
        project: ProjectRecord,
        resolution: Resolution,
        verification_mode: VerificationMode | None,
    ) -> StatusOutcome:
        snapshot_currentness = CurrentnessState.UNVERIFIED
        try:
            repo_evaluation = check_registered_repository(project, registry=self.registry)
        except RegistryOperationError as error:
            return self._registry_problem(resolution, error, project=project)

        project = repo_evaluation.project
        if repo_evaluation.state is not None:
            return self._problem(
                repo_evaluation.state,
                resolution=resolution,
                project=project,
                repo_check=repo_evaluation.check,
            )

        storage_check, storage_state = check_project_storage(project, home=self.home)
        if storage_state is not None:
            return self._problem(
                storage_state,
                resolution=resolution,
                project=project,
                repo_check=repo_evaluation.check,
                storage_check=storage_check,
            )

        snapshot_path = Path(project.storage_path) / "kb.sqlite"
        if not snapshot_path.exists():
            state = ProjectState.REGISTERED_NO_SNAPSHOT
            snapshot_check = SnapshotCheck.absent()
            snapshot_present = False
        else:
            try:
                snapshot_meta = validate_snapshot(
                    snapshot_path,
                    project_id=project.project_id,
                    expected_repo_root_norm=project.repo_root_norm,
                    expected_repository_identity_hash=repository_identity_hash(
                        project.repo_root_norm,
                        project.repo_fingerprint_json,
                    ),
                    expected_repository_binding_generation=(project.repo_binding_generation),
                )
            except SnapshotQueryError as error:
                state = (
                    ProjectState.SNAPSHOT_REBUILD_REQUIRED
                    if error.code == "SNAPSHOT_REBUILD_REQUIRED"
                    else ProjectState.SNAPSHOT_STORAGE_ERROR
                )
                return self._problem(
                    state,
                    resolution=resolution,
                    project=project,
                    repo_check=repo_evaluation.check,
                    storage_check=storage_check,
                    snapshot_check=SnapshotCheck.not_checked(
                        "snapshot_repository_binding_mismatch"
                        if error.details.get("snapshot_classification")
                        == "wrong_repository_binding"
                        else "snapshot_validation_failed",
                        availability=(
                            "INCOMPATIBLE"
                            if error.code == "SNAPSHOT_REBUILD_REQUIRED"
                            else "CORRUPT"
                        ),
                        compatibility=(
                            "INCOMPATIBLE"
                            if error.code == "SNAPSHOT_REBUILD_REQUIRED"
                            else "NOT_CHECKED"
                        ),
                    ),
                    error_details=error.details,
                )
            if (
                snapshot_meta["schema_version"] != SCHEMA_VERSION
                and project.snapshot_binding_generation != project.repo_binding_generation
            ):
                return self._problem(
                    ProjectState.SNAPSHOT_REBUILD_REQUIRED,
                    resolution=resolution,
                    project=project,
                    repo_check=repo_evaluation.check,
                    storage_check=storage_check,
                    snapshot_check=SnapshotCheck.not_checked(
                        "snapshot_repository_binding_mismatch",
                        availability="INCOMPATIBLE",
                        compatibility="INCOMPATIBLE",
                    ),
                    error_details={
                        "snapshot_classification": "wrong_repository_binding",
                        "reason": "registration_generation_changed_since_legacy_snapshot",
                    },
                )
            registry_reconciled = (
                project.last_status == "INDEX_SUCCEEDED"
                and project.last_indexed_at == snapshot_meta["created_at"]
            )
            if registry_reconciled:
                state = ProjectState.SNAPSHOT_PRESENT_UNVERIFIED
            elif _is_newer(snapshot_meta["created_at"], project.updated_at):
                state = ProjectState.SNAPSHOT_PRESENT_REGISTRY_WARNING
            elif (project.last_status or "").startswith("INDEX_FAILED:"):
                state = ProjectState.LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE
            else:
                state = ProjectState.SNAPSHOT_PRESENT_REGISTRY_WARNING
            compatibility = (
                "COMPATIBLE" if snapshot_meta["schema_version"] == SCHEMA_VERSION else "LEGACY_V1"
            )
            snapshot_check = SnapshotCheck(
                status="valid_when_published_currentness_unverified",
                snapshot_id=snapshot_meta["snapshot_id"],
                indexed_at=snapshot_meta["created_at"],
                git_commit_at_index=snapshot_meta["git_head"],
                current_git_commit=None,
                is_current=(False if snapshot_meta["schema_version"] != SCHEMA_VERSION else None),
                reason=(
                    "present_working_tree_not_compared"
                    if state is ProjectState.SNAPSHOT_PRESENT_UNVERIFIED
                    else "snapshot_available_with_registry_outcome_warning"
                ),
                availability="AVAILABLE",
                compatibility=compatibility,
                truth_claim=(
                    "CAPTURED_STABLE" if snapshot_meta["schema_version"] == SCHEMA_VERSION else None
                ),
            )
            if verification_mode is not None:
                if snapshot_meta["schema_version"] != SCHEMA_VERSION:
                    state = ProjectState.SNAPSHOT_LEGACY_REINDEX_REQUIRED
                    snapshot_check = SnapshotCheck(
                        status="legacy_snapshot_currentness_unavailable",
                        snapshot_id=snapshot_meta["snapshot_id"],
                        indexed_at=snapshot_meta["created_at"],
                        git_commit_at_index=snapshot_meta["git_head"],
                        current_git_commit=None,
                        is_current=False,
                        reason="legacy_v1_reindex_required_for_currentness",
                        availability="AVAILABLE",
                        compatibility="LEGACY_V1",
                        currentness=CurrentnessState.UNVERIFIED.value,
                        verification_mode=verification_mode.value,
                    )
                else:
                    expected_binding = (
                        project.repo_root_norm,
                        repository_identity_hash(
                            project.repo_root_norm,
                            project.repo_fingerprint_json,
                        ),
                        project.repo_binding_generation,
                    )

                    def active_binding() -> RepositoryBindingObservation:
                        return self._repository_binding_observation(project)

                    verification = verify_snapshot_currentness(
                        snapshot_path,
                        repo_root=Path(project.repo_root),
                        snapshot_meta=snapshot_meta,
                        mode=verification_mode,
                        active_binding=active_binding,
                        expected_binding=expected_binding,
                    )
                    snapshot_currentness = verification.state
                    snapshot_check = SnapshotCheck(
                        status="currentness_verification_completed",
                        snapshot_id=snapshot_meta["snapshot_id"],
                        indexed_at=snapshot_meta["created_at"],
                        git_commit_at_index=snapshot_meta["git_head"],
                        current_git_commit=verification.current_git_commit,
                        is_current=None,
                        reason=verification.reason,
                        availability="AVAILABLE",
                        compatibility="COMPATIBLE",
                        currentness=verification.state.value,
                        truth_claim=(
                            "CURRENT_AT_VERIFIED_TIME"
                            if verification.state is CurrentnessState.CURRENT
                            else "CAPTURED_STABLE"
                        ),
                        verification_mode=verification.mode.value,
                        verified_at=verification.verified_at,
                        verification_duration_ms=verification.duration_ms,
                        verification_timings_ms=verification.timings_ms,
                        verification_attempts=verification.attempts,
                        mismatch_paths=verification.mismatch_paths,
                        deltas=verification.deltas,
                        diagnostics=verification.diagnostics,
                        exclusions=verification.exclusions,
                        verification_scope=json.loads(snapshot_meta["verification_scope_json"]),
                        proof_contract_version=snapshot_meta["proof_contract_version"],
                        verifier_version=snapshot_meta["verifier_version"],
                    )
                    if verification.binding_status == "BINDING_MISMATCH":
                        active_project = self.registry.find_project_by_name(project.project_name)
                        return self._problem(
                            ProjectState.SNAPSHOT_REBUILD_REQUIRED,
                            resolution=resolution,
                            project=active_project or project,
                            repo_check=repo_evaluation.check,
                            storage_check=storage_check,
                            snapshot_check=replace(
                                snapshot_check,
                                availability="INCOMPATIBLE",
                                compatibility="INCOMPATIBLE",
                            ),
                            error_details={
                                "snapshot_classification": "wrong_repository_binding",
                                "reason": "binding_changed_during_verification",
                            },
                        )
                    if verification.binding_status == "IDENTITY_MISMATCH":
                        active_project = self.registry.find_project_by_name(project.project_name)
                        if active_project is not None:
                            identity_evaluation = check_registered_repository(
                                active_project,
                                registry=self.registry,
                            )
                            if identity_evaluation.state is not None:
                                return self._problem(
                                    identity_evaluation.state,
                                    resolution=resolution,
                                    project=identity_evaluation.project,
                                    repo_check=identity_evaluation.check,
                                    storage_check=storage_check,
                                    snapshot_check=snapshot_check,
                                    error_details={
                                        "reason": "identity_changed_during_verification"
                                    },
                                )
                        state = ProjectState.SNAPSHOT_CHANGED_DURING_CHECK
                    if verification.state is CurrentnessState.CURRENT:
                        if state is ProjectState.SNAPSHOT_PRESENT_UNVERIFIED:
                            state = ProjectState.OK
                    elif verification.state is CurrentnessState.STALE:
                        state = ProjectState.SNAPSHOT_STALE
                    elif verification.state is CurrentnessState.CHANGED_DURING_CHECK:
                        state = ProjectState.SNAPSHOT_CHANGED_DURING_CHECK
                    elif verification.state is CurrentnessState.ERROR:
                        state = ProjectState.SNAPSHOT_VERIFICATION_ERROR
            snapshot_present = True
        policy = STATE_POLICIES[state]
        action = recommended_action_for(
            state,
            project_name=project.project_name,
            repo_root=project.repo_root,
        )
        return StatusOutcome(
            ok=policy.ok,
            result=policy.result,
            code=policy.code,
            message=policy.message,
            exit_code=policy.exit_code,
            project_state=state,
            resolution=resolution,
            project=project,
            repo_check=repo_evaluation.check,
            storage_check=storage_check,
            snapshot_check=snapshot_check,
            availability=_availability(
                registry_available=True,
                project_resolved=True,
                repo_valid=True,
                storage_valid=True,
                snapshot_present=snapshot_present,
                snapshot_currentness=snapshot_currentness,
            ),
            requires_user_action=policy.requires_user_action,
            recommended_action=action,
            error=None,
        )

    def _fast_verification_removed(self, project_name: str | None) -> StatusOutcome:
        return self._problem(
            ProjectState.FAST_VERIFICATION_REMOVED,
            resolution=Resolution(
                mode="name" if project_name is not None else "current_directory",
                status="verification_removed",
                project_name_input=project_name,
                working_directory=str(self.working_directory),
                resolved_repo_root=None,
                resolved_by=None,
            ),
            snapshot_check=SnapshotCheck(
                status="verification_removed",
                snapshot_id=None,
                indexed_at=None,
                git_commit_at_index=None,
                current_git_commit=None,
                is_current=None,
                reason="fast_verification_removed",
                availability="NOT_CHECKED",
                compatibility="NOT_CHECKED",
                currentness=CurrentnessState.UNVERIFIED.value,
                verification_mode=VerificationMode.FAST.value,
            ),
        )

    def _repository_binding_observation(
        self,
        project: ProjectRecord,
    ) -> RepositoryBindingObservation:
        current = self.registry.find_project_by_name(project.project_name)
        if current is None or current.project_id != project.project_id:
            return RepositoryBindingObservation(
                repo_root_norm="<registration-missing>",
                repository_identity_hash="<registration-missing>",
                repository_binding_generation="<registration-missing>",
                live_identity_token="<registration-missing>",
                live_identity_matches=False,
                live_identity_reason="registration_missing",
            )
        if current.repo_fingerprint_json is None:
            raise ValueError("active repository fingerprint is unavailable")
        return observe_repository_binding(
            repo_root=Path(current.repo_root),
            repo_root_norm=current.repo_root_norm,
            repository_fingerprint_json=current.repo_fingerprint_json,
            repository_binding_generation=current.repo_binding_generation,
        )

    def _registry_problem(
        self,
        resolution: Resolution,
        error: RegistryOperationError,
        *,
        project: ProjectRecord | None = None,
    ) -> StatusOutcome:
        return self._problem(
            ProjectState.REGISTRY_ERROR,
            resolution=Resolution(
                mode=resolution.mode,
                status="failed",
                project_name_input=resolution.project_name_input,
                working_directory=resolution.working_directory,
                resolved_repo_root=resolution.resolved_repo_root,
                resolved_by=resolution.resolved_by,
            ),
            project=project,
            error_details=error.details,
        )

    def _problem(
        self,
        state: ProjectState,
        *,
        resolution: Resolution,
        project: ProjectRecord | None = None,
        repo_check: RepoCheck | None = None,
        storage_check: StorageCheck | None = None,
        snapshot_check: SnapshotCheck | None = None,
        code: str | None = None,
        message: str | None = None,
        exit_code: int | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> StatusOutcome:
        policy = STATE_POLICIES[state]
        outcome_code = code or policy.code
        outcome_message = message or policy.message
        outcome_exit_code = exit_code if exit_code is not None else policy.exit_code
        action = recommended_action_for(
            state,
            project_name=project.project_name if project else resolution.project_name_input,
            repo_root=project.repo_root if project else resolution.resolved_repo_root,
            outcome_code=outcome_code,
        )
        details = {"project_state": state.value}
        if error_details:
            details.update(error_details)
        error = (
            ProjectStatusError(
                code=outcome_code,
                message=outcome_message,
                exit_code=outcome_exit_code,
                recommended_action=action.code,
                details=details,
            )
            if policy.has_error
            else None
        )
        return StatusOutcome(
            ok=policy.ok,
            result=policy.result,
            code=outcome_code,
            message=outcome_message,
            exit_code=outcome_exit_code,
            project_state=state,
            resolution=resolution,
            project=project,
            repo_check=repo_check or RepoCheck.not_checked("project_not_available"),
            storage_check=storage_check or StorageCheck.not_checked("repository_not_valid"),
            snapshot_check=snapshot_check or SnapshotCheck.not_checked("project_not_usable"),
            availability=Availability.unavailable(),
            requires_user_action=policy.requires_user_action,
            recommended_action=action,
            error=error,
        )


def _availability(
    *,
    registry_available: bool,
    project_resolved: bool,
    repo_valid: bool,
    storage_valid: bool,
    snapshot_present: bool,
    snapshot_currentness: CurrentnessState,
) -> Availability:
    context = GateContext(
        registry_available=registry_available,
        project_resolved=project_resolved,
        repo_valid=repo_valid,
        storage_valid=storage_valid,
        snapshot_present=snapshot_present,
        snapshot_currentness=snapshot_currentness.value,
    )
    project_requirements = (
        GateRequirement.REGISTRY_AVAILABLE,
        GateRequirement.PROJECT_RESOLVED,
        GateRequirement.REPO_VALID,
        GateRequirement.STORAGE_VALID,
    )
    snapshot_requirements = (*project_requirements, GateRequirement.SNAPSHOT_PRESENT)
    current_snapshot_requirements = (
        *snapshot_requirements,
        GateRequirement.SNAPSHOT_CURRENT,
    )
    return Availability(
        can_use_project=evaluate_gate(context, project_requirements).allowed,
        can_use_snapshot=evaluate_gate(context, snapshot_requirements).allowed,
        can_search=evaluate_gate(context, current_snapshot_requirements).allowed,
        can_generate_exports=evaluate_gate(context, current_snapshot_requirements).allowed,
        can_generate_context=evaluate_gate(context, current_snapshot_requirements).allowed,
    )


def _is_newer(left: str, right: str) -> bool:
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) > datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )
    except ValueError:
        return False
