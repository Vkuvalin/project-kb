"""Registered repository health and identity checks."""

from dataclasses import dataclass
from pathlib import Path

from project_kb.errors import NotGitRepositoryError, RegistryOperationError
from project_kb.git_utils import resolve_git_root
from project_kb.registry.models import ProjectRecord
from project_kb.registry.service import RegistryService, normalize_repo_root
from project_kb.resolver.models import RepoCheck
from project_kb.resolver.repo_identity import (
    RepositoryFingerprint,
    build_repository_fingerprint,
    compare_repository_fingerprints,
)
from project_kb.resolver.state import ProjectState


@dataclass(frozen=True)
class RepositoryEvaluation:
    project: ProjectRecord
    check: RepoCheck
    state: ProjectState | None


def check_registered_repository(
    project: ProjectRecord,
    *,
    registry: RegistryService,
) -> RepositoryEvaluation:
    """Run ordered repository checks and initialize legacy fingerprint metadata."""

    stored_root = Path(project.repo_root).expanduser()
    path_exists = stored_root.exists()
    if not path_exists:
        return RepositoryEvaluation(
            project,
            RepoCheck(
                status="invalid",
                stored_repo_root=project.repo_root,
                current_git_root=None,
                path_exists=False,
                is_directory=False,
                is_git_repository=False,
                git_root_matches=None,
                fingerprint_status="not_checked",
                fingerprint_strength=None,
                reason="registered_repo_path_missing",
            ),
            ProjectState.BROKEN_PATH,
        )

    is_directory = stored_root.is_dir()
    if not is_directory:
        return RepositoryEvaluation(
            project,
            RepoCheck(
                status="invalid",
                stored_repo_root=project.repo_root,
                current_git_root=None,
                path_exists=True,
                is_directory=False,
                is_git_repository=False,
                git_root_matches=None,
                fingerprint_status="not_checked",
                fingerprint_strength=None,
                reason="registered_repo_path_not_directory",
            ),
            ProjectState.BROKEN_PATH,
        )

    try:
        current_root = resolve_git_root(stored_root)
    except NotGitRepositoryError:
        return RepositoryEvaluation(
            project,
            RepoCheck(
                status="invalid",
                stored_repo_root=project.repo_root,
                current_git_root=None,
                path_exists=True,
                is_directory=True,
                is_git_repository=False,
                git_root_matches=None,
                fingerprint_status="not_checked",
                fingerprint_strength=None,
                reason="registered_path_not_git_repository",
            ),
            ProjectState.NOT_A_GIT_REPOSITORY,
        )

    git_root_matches = normalize_repo_root(current_root) == project.repo_root_norm
    if not git_root_matches:
        return RepositoryEvaluation(
            project,
            RepoCheck(
                status="invalid",
                stored_repo_root=project.repo_root,
                current_git_root=str(current_root),
                path_exists=True,
                is_directory=True,
                is_git_repository=True,
                git_root_matches=False,
                fingerprint_status="not_checked",
                fingerprint_strength=None,
                reason="git_root_mismatch",
            ),
            ProjectState.REPO_MISMATCH,
        )

    current_fingerprint = build_repository_fingerprint(current_root)
    if project.repo_fingerprint_json is None:
        project, initialized = registry.initialize_repo_fingerprint(
            project.project_id,
            current_fingerprint,
        )
        if initialized:
            return RepositoryEvaluation(
                project,
                _valid_repo_check(
                    project,
                    current_root,
                    fingerprint_status="initialized",
                    fingerprint_strength=current_fingerprint.fingerprint_strength,
                    reason="fingerprint_initialized",
                ),
                None,
            )

    stored_fingerprint = _load_fingerprint(project)
    comparison = compare_repository_fingerprints(
        stored_fingerprint,
        current_fingerprint,
        repo_root=current_root,
    )
    if not comparison.matches:
        return RepositoryEvaluation(
            project,
            RepoCheck(
                status="invalid",
                stored_repo_root=project.repo_root,
                current_git_root=str(current_root),
                path_exists=True,
                is_directory=True,
                is_git_repository=True,
                git_root_matches=True,
                fingerprint_status=comparison.status,
                fingerprint_strength=stored_fingerprint.fingerprint_strength,
                reason=comparison.reason,
            ),
            ProjectState.REPO_MISMATCH,
        )

    return RepositoryEvaluation(
        project,
        _valid_repo_check(
            project,
            current_root,
            fingerprint_status=comparison.status,
            fingerprint_strength=stored_fingerprint.fingerprint_strength,
            reason=comparison.reason,
        ),
        None,
    )


def _load_fingerprint(project: ProjectRecord) -> RepositoryFingerprint:
    if project.repo_fingerprint_json is None:
        raise RegistryOperationError(
            "Repository fingerprint initialization did not persist metadata.",
            details={"project_id": project.project_id},
        )
    try:
        return RepositoryFingerprint.from_json(project.repo_fingerprint_json)
    except ValueError as exc:
        raise RegistryOperationError(
            "Stored repository fingerprint metadata is invalid.",
            details={"project_id": project.project_id},
        ) from exc


def _valid_repo_check(
    project: ProjectRecord,
    current_root: Path,
    *,
    fingerprint_status: str,
    fingerprint_strength: str,
    reason: str,
) -> RepoCheck:
    return RepoCheck(
        status="valid",
        stored_repo_root=project.repo_root,
        current_git_root=str(current_root),
        path_exists=True,
        is_directory=True,
        is_git_repository=True,
        git_root_matches=True,
        fingerprint_status=fingerprint_status,
        fingerprint_strength=fingerprint_strength,
        reason=reason,
    )
