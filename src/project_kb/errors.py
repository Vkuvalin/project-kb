"""Project KB error model."""

from typing import Any

from project_kb.exit_codes import INTERNAL_ERROR, NOT_IMPLEMENTED


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
