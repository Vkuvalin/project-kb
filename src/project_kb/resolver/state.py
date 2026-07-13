"""Central Stage 3 project-state and recovery policy."""

import re
from dataclasses import dataclass
from enum import StrEnum

from project_kb.exit_codes import (
    GIT_REPO_ERROR,
    OK,
    PROJECT_NOT_REGISTERED,
    REGISTRY_ERROR,
    REPO_PATH_ERROR,
    REPOSITORY_MISMATCH,
    SNAPSHOT_UNAVAILABLE,
    STORAGE_STATE_ERROR,
)
from project_kb.gating.models import RecommendedAction


class ProjectState(StrEnum):
    REGISTERED_NO_SNAPSHOT = "REGISTERED_NO_SNAPSHOT"
    SNAPSHOT_PRESENT_UNVERIFIED = "SNAPSHOT_PRESENT_UNVERIFIED"
    SNAPSHOT_PRESENT_REGISTRY_WARNING = "SNAPSHOT_PRESENT_REGISTRY_WARNING"
    LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE = "LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE"
    SNAPSHOT_REBUILD_REQUIRED = "SNAPSHOT_REBUILD_REQUIRED"
    SNAPSHOT_STORAGE_ERROR = "SNAPSHOT_STORAGE_ERROR"
    OK = "OK"
    UNREGISTERED = "UNREGISTERED"
    BROKEN_PATH = "BROKEN_PATH"
    NOT_A_GIT_REPOSITORY = "NOT_A_GIT_REPOSITORY"
    REPO_MISMATCH = "REPO_MISMATCH"
    STORAGE_MISSING = "STORAGE_MISSING"
    STORAGE_MISMATCH = "STORAGE_MISMATCH"
    REGISTRY_ERROR = "REGISTRY_ERROR"


@dataclass(frozen=True)
class StatePolicy:
    code: str
    message: str
    exit_code: int
    ok: bool
    result: str
    has_error: bool
    requires_user_action: bool


STATE_POLICIES = {
    ProjectState.REGISTERED_NO_SNAPSHOT: StatePolicy(
        code="REGISTERED_NO_SNAPSHOT",
        message="Project is registered and valid, but no snapshot has been created.",
        exit_code=SNAPSHOT_UNAVAILABLE,
        ok=False,
        result="blocked",
        has_error=False,
        requires_user_action=True,
    ),
    ProjectState.SNAPSHOT_PRESENT_UNVERIFIED: StatePolicy(
        code="SNAPSHOT_PRESENT_UNVERIFIED",
        message=(
            "A valid published snapshot is available; present working-tree "
            "currentness is unverified."
        ),
        exit_code=OK,
        ok=True,
        result="success",
        has_error=False,
        requires_user_action=False,
    ),
    ProjectState.LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE: StatePolicy(
        code="LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE",
        message="The last index attempt failed; the previous valid snapshot remains available.",
        exit_code=OK,
        ok=True,
        result="success_with_warnings",
        has_error=False,
        requires_user_action=True,
    ),
    ProjectState.SNAPSHOT_PRESENT_REGISTRY_WARNING: StatePolicy(
        code="SNAPSHOT_PRESENT_REGISTRY_WARNING",
        message="A newer published snapshot is available, but registry outcome metadata is stale.",
        exit_code=OK,
        ok=True,
        result="success_with_warnings",
        has_error=False,
        requires_user_action=True,
    ),
    ProjectState.SNAPSHOT_REBUILD_REQUIRED: StatePolicy(
        code="SNAPSHOT_REBUILD_REQUIRED",
        message="The snapshot is readable but incompatible with current structural semantics.",
        exit_code=SNAPSHOT_UNAVAILABLE,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.SNAPSHOT_STORAGE_ERROR: StatePolicy(
        code="SNAPSHOT_STORAGE_ERROR",
        message="The project snapshot exists but could not be validated safely.",
        exit_code=SNAPSHOT_UNAVAILABLE,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.OK: StatePolicy(
        code="OK",
        message="Project and snapshot are available.",
        exit_code=OK,
        ok=True,
        result="success",
        has_error=False,
        requires_user_action=False,
    ),
    ProjectState.UNREGISTERED: StatePolicy(
        code="PROJECT_NOT_REGISTERED",
        message="Project is not registered.",
        exit_code=PROJECT_NOT_REGISTERED,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.BROKEN_PATH: StatePolicy(
        code="BROKEN_PATH",
        message="The registered repository path is missing or is not a directory.",
        exit_code=REPO_PATH_ERROR,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.NOT_A_GIT_REPOSITORY: StatePolicy(
        code="NOT_A_GIT_REPOSITORY",
        message="The registered repository path is not a Git repository.",
        exit_code=GIT_REPO_ERROR,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.REPO_MISMATCH: StatePolicy(
        code="REPO_MISMATCH",
        message="The repository at the registered path no longer matches its identity.",
        exit_code=REPOSITORY_MISMATCH,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.STORAGE_MISSING: StatePolicy(
        code="STORAGE_MISSING",
        message="Required Project KB storage directories are missing.",
        exit_code=STORAGE_STATE_ERROR,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.STORAGE_MISMATCH: StatePolicy(
        code="STORAGE_MISMATCH",
        message="Registered Project KB storage does not match the expected path.",
        exit_code=STORAGE_STATE_ERROR,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
    ProjectState.REGISTRY_ERROR: StatePolicy(
        code="REGISTRY_ERROR",
        message="The Project KB registry could not be read safely.",
        exit_code=REGISTRY_ERROR,
        ok=False,
        result="blocked",
        has_error=True,
        requires_user_action=True,
    ),
}


def recommended_action_for(
    state: ProjectState,
    *,
    project_name: str | None = None,
    repo_root: str | None = None,
    outcome_code: str | None = None,
) -> RecommendedAction:
    name = _command_argument(project_name, placeholder="<name>")
    root = _command_argument(repo_root, placeholder="<repo_path>")

    if outcome_code == "CURRENT_DIRECTORY_NOT_GIT_REPO":
        return RecommendedAction(
            code="OPEN_GIT_REPOSITORY",
            command=None,
            available=False,
            requires_user_approval=False,
            reason="current_directory_not_git_repository",
        )
    if outcome_code == "INVALID_PROJECT_NAME":
        return RecommendedAction(
            code="CHOOSE_VALID_PROJECT_NAME",
            command=None,
            available=False,
            requires_user_approval=False,
            reason="project_name_invalid",
        )

    actions = {
        ProjectState.REGISTERED_NO_SNAPSHOT: RecommendedAction(
            code="RUN_INDEX",
            command=f"pkb index {name} --json",
            available=False,
            requires_user_approval=True,
            reason="snapshot_not_created",
        ),
        ProjectState.SNAPSHOT_PRESENT_UNVERIFIED: RecommendedAction(
            code="NONE",
            command=None,
            available=True,
            requires_user_approval=False,
            reason="snapshot_valid_when_published_currentness_unverified",
        ),
        ProjectState.LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE: RecommendedAction(
            code="RETRY_INDEX",
            command=f"pkb index {name} --json",
            available=True,
            requires_user_approval=False,
            reason="last_index_failed_previous_snapshot_available",
        ),
        ProjectState.SNAPSHOT_PRESENT_REGISTRY_WARNING: RecommendedAction(
            code="RECONCILE_REGISTRY_OR_REINDEX",
            command=f"pkb index {name} --full --json",
            available=True,
            requires_user_approval=False,
            reason="published_snapshot_newer_than_registry_outcome",
        ),
        ProjectState.SNAPSHOT_REBUILD_REQUIRED: RecommendedAction(
            code="REBUILD_INDEX",
            command=f"pkb index {name} --full --json",
            available=True,
            requires_user_approval=False,
            reason="snapshot_semantics_incompatible",
        ),
        ProjectState.SNAPSHOT_STORAGE_ERROR: RecommendedAction(
            code="REBUILD_INDEX",
            command=f"pkb index {name} --full --json",
            available=True,
            requires_user_approval=False,
            reason="snapshot_storage_invalid",
        ),
        ProjectState.OK: RecommendedAction(
            code="NONE",
            command=None,
            available=True,
            requires_user_approval=False,
            reason="project_ready",
        ),
        ProjectState.UNREGISTERED: RecommendedAction(
            code="REGISTER_PROJECT",
            command=f"pkb register {name} {root} --json",
            available=True,
            requires_user_approval=True,
            reason="project_not_registered",
        ),
        ProjectState.BROKEN_PATH: RecommendedAction(
            code="RELINK_OR_UNREGISTER",
            command=f"pkb relink {name} <new_repo_path> --json",
            available=True,
            requires_user_approval=True,
            reason="registered_path_broken",
        ),
        ProjectState.NOT_A_GIT_REPOSITORY: RecommendedAction(
            code="RESTORE_GIT_OR_RELINK",
            command=f"pkb relink {name} <new_repo_path> --json",
            available=True,
            requires_user_approval=True,
            reason="registered_path_not_git_repository",
        ),
        ProjectState.REPO_MISMATCH: RecommendedAction(
            code="VERIFY_AND_RELINK",
            command=f"pkb relink {name} <verified_repo_path> --json",
            available=True,
            requires_user_approval=True,
            reason="repository_identity_mismatch",
        ),
        ProjectState.STORAGE_MISSING: RecommendedAction(
            code="REFRESH_REGISTRATION",
            command=f"pkb register {name} {root} --json",
            available=True,
            requires_user_approval=True,
            reason="required_storage_missing",
        ),
        ProjectState.STORAGE_MISMATCH: RecommendedAction(
            code="MANUAL_STORAGE_INSPECTION",
            command=None,
            available=False,
            requires_user_approval=True,
            reason="registered_storage_path_mismatch",
        ),
        ProjectState.REGISTRY_ERROR: RecommendedAction(
            code="INSPECT_REGISTRY",
            command=None,
            available=False,
            requires_user_approval=True,
            reason="registry_unavailable_or_invalid",
        ),
    }
    return actions[state]


def _command_argument(value: str | None, *, placeholder: str) -> str:
    """Format a known value as a safe PowerShell command argument."""

    if value is None:
        return placeholder
    if re.fullmatch(r"[a-zA-Z0-9._:/\\-]+", value):
        return value
    return "'" + value.replace("'", "''") + "'"
