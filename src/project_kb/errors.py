"""Project KB error model."""

from typing import Any

from project_kb.exit_codes import (
    DESTRUCTIVE_CONFIRMATION_REQUIRED,
    GIT_REPO_ERROR,
    INTERNAL_ERROR,
    NOT_IMPLEMENTED,
    PROJECT_NOT_REGISTERED,
    REGISTRY_ERROR,
    REPO_PATH_ERROR,
    USAGE_ERROR,
)


class ProjectKbError(Exception):
    """Base structured error for Project KB."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        exit_code: int = INTERNAL_ERROR,
        recommended_action: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.recommended_action = recommended_action
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Return the public JSON error representation."""

        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "recommended_action": self.recommended_action,
            "retryable": self.retryable,
            "details": self.details,
        }


class NotImplementedFeatureError(ProjectKbError):
    """Raised when a command is known but intentionally not implemented yet."""

    def __init__(
        self,
        message: str = "Project resolver will be implemented in a later stage.",
        *,
        recommended_action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="NOT_IMPLEMENTED",
            message=message,
            exit_code=NOT_IMPLEMENTED,
            recommended_action=recommended_action,
            retryable=False,
            details=details,
        )


class InvalidProjectNameError(ProjectKbError):
    """Raised when a project name does not match the Stage 2 CLI alias rules."""

    def __init__(self, project_name: str) -> None:
        super().__init__(
            code="INVALID_PROJECT_NAME",
            message=(
                "Invalid project name. Use 1-80 ASCII letters, digits, dots, "
                "underscores, or hyphens, starting with a letter or digit."
            ),
            exit_code=USAGE_ERROR,
            recommended_action="Choose a name matching [a-zA-Z0-9][a-zA-Z0-9._-]{0,79}.",
            retryable=False,
            details={"project_name": project_name},
        )


class ProjectNameAlreadyUsedError(ProjectKbError):
    """Raised when a project name already points at another repository."""

    def __init__(self, project_name: str, repo_root: str) -> None:
        super().__init__(
            code="PROJECT_NAME_ALREADY_USED",
            message=f"Project name '{project_name}' is already registered.",
            exit_code=REGISTRY_ERROR,
            recommended_action="Use a different name or run relink for this project.",
            retryable=False,
            details={"project_name": project_name, "repo_root": repo_root},
        )


class RepoAlreadyRegisteredError(ProjectKbError):
    """Raised when a Git root is already registered under another name."""

    def __init__(self, repo_root: str, project_name: str) -> None:
        super().__init__(
            code="REPO_ALREADY_REGISTERED",
            message=f"Repository is already registered as '{project_name}'.",
            exit_code=REGISTRY_ERROR,
            recommended_action="Use the existing project name or unregister it first.",
            retryable=False,
            details={"repo_root": repo_root, "project_name": project_name},
        )


class ProjectNotRegisteredError(ProjectKbError):
    """Raised when a requested project name is not in the registry."""

    def __init__(self, project_name: str) -> None:
        super().__init__(
            code="PROJECT_NOT_REGISTERED",
            message=f"Project '{project_name}' is not registered.",
            exit_code=PROJECT_NOT_REGISTERED,
            recommended_action="Register the project first.",
            retryable=False,
            details={"project_name": project_name},
        )


class RepoPathNotFoundError(ProjectKbError):
    """Raised when a requested repository path does not exist."""

    def __init__(self, repo_path: str) -> None:
        super().__init__(
            code="REPO_PATH_NOT_FOUND",
            message=f"Repository path does not exist: {repo_path}",
            exit_code=REPO_PATH_ERROR,
            recommended_action="Pass an existing Git repository path.",
            retryable=False,
            details={"repo_path": repo_path},
        )


class RepoPathNotDirectoryError(ProjectKbError):
    """Raised when a requested repository path is not a directory."""

    def __init__(self, repo_path: str) -> None:
        super().__init__(
            code="REPO_PATH_NOT_DIRECTORY",
            message=f"Repository path is not a directory: {repo_path}",
            exit_code=REPO_PATH_ERROR,
            recommended_action="Pass a Git repository directory.",
            retryable=False,
            details={"repo_path": repo_path},
        )


class NotGitRepositoryError(ProjectKbError):
    """Raised when Git cannot resolve a repository top-level path."""

    def __init__(self, repo_path: str) -> None:
        super().__init__(
            code="NOT_A_GIT_REPOSITORY",
            message=f"Path is not inside a Git repository: {repo_path}",
            exit_code=GIT_REPO_ERROR,
            recommended_action="Initialize a Git repository or pass a path inside one.",
            retryable=False,
            details={"repo_path": repo_path},
        )


class RegistryOperationError(ProjectKbError):
    """Raised when registry storage or SQLite operations fail."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code="REGISTRY_ERROR",
            message=message,
            exit_code=REGISTRY_ERROR,
            recommended_action="Retry the command or inspect the registry file.",
            retryable=True,
            details=details,
        )


class ProjectStatusError(ProjectKbError):
    """Structured problem associated with a classified status outcome."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        exit_code: int,
        recommended_action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            exit_code=exit_code,
            recommended_action=recommended_action,
            retryable=False,
            details=details,
        )


class UnregisterRequiresYesError(ProjectKbError):
    """Raised when unregister is requested without explicit confirmation."""

    def __init__(self, project_name: str) -> None:
        super().__init__(
            code="UNREGISTER_REQUIRES_YES",
            message="Unregister requires --yes because it removes Project KB storage.",
            exit_code=DESTRUCTIVE_CONFIRMATION_REQUIRED,
            recommended_action="Re-run with --yes to remove this project from Project KB tracking.",
            retryable=False,
            details={"project_name": project_name},
        )
