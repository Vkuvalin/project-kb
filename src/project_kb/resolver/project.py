"""Project status resolution orchestration."""

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
from project_kb.resolver.state import (
    STATE_POLICIES,
    ProjectState,
    recommended_action_for,
)
from project_kb.resolver.storage_check import check_project_storage
from project_kb.snapshot.database import validate_snapshot
from project_kb.storage.home import resolve_home


class ProjectStatusService:
    """Resolve one project and produce the complete Stage 3 status contract."""

    def __init__(self, *, home: Path | None = None, working_directory: Path | None = None) -> None:
        self.home = home or resolve_home()
        self.working_directory = (working_directory or Path.cwd()).resolve()
        self.registry = RegistryService(home=self.home)

    def status(self, project_name: str | None = None) -> StatusOutcome:
        if project_name is not None:
            return self._status_by_name(project_name)
        return self._status_by_current_directory()

    def _status_by_name(self, project_name: str) -> StatusOutcome:
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
        )

    def _status_by_current_directory(self) -> StatusOutcome:
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
        )

    def _status_resolved_project(
        self,
        project: ProjectRecord,
        resolution: Resolution,
    ) -> StatusOutcome:
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
                snapshot_meta = validate_snapshot(snapshot_path, project_id=project.project_id)
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
                    error_details=error.details,
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
            snapshot_check = SnapshotCheck(
                status="valid_when_published_currentness_unverified",
                snapshot_id=snapshot_meta["snapshot_id"],
                indexed_at=snapshot_meta["created_at"],
                git_commit_at_index=snapshot_meta["git_head"],
                current_git_commit=None,
                is_current=False,
                reason=(
                    "present_working_tree_not_compared"
                    if state is ProjectState.SNAPSHOT_PRESENT_UNVERIFIED
                    else "snapshot_available_with_registry_outcome_warning"
                ),
            )
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
                snapshot_current=False,
            ),
            requires_user_action=policy.requires_user_action,
            recommended_action=action,
            error=None,
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
            snapshot_check=SnapshotCheck.not_checked("project_not_usable"),
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
    snapshot_current: bool,
) -> Availability:
    context = GateContext(
        registry_available=registry_available,
        project_resolved=project_resolved,
        repo_valid=repo_valid,
        storage_valid=storage_valid,
        snapshot_present=snapshot_present,
        snapshot_current=snapshot_current,
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
