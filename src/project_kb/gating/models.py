"""Typed models for the small Stage 3 command-gating foundation."""

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class GateRequirement(StrEnum):
    REGISTRY_AVAILABLE = "REGISTRY_AVAILABLE"
    PROJECT_RESOLVED = "PROJECT_RESOLVED"
    REPO_VALID = "REPO_VALID"
    STORAGE_VALID = "STORAGE_VALID"
    SNAPSHOT_PRESENT = "SNAPSHOT_PRESENT"
    SNAPSHOT_CURRENT = "SNAPSHOT_CURRENT"
    USER_APPROVAL = "USER_APPROVAL"


@dataclass(frozen=True)
class RecommendedAction:
    code: str
    command: str | None
    available: bool
    requires_user_approval: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateContext:
    registry_available: bool = False
    project_resolved: bool = False
    repo_valid: bool = False
    storage_valid: bool = False
    snapshot_present: bool = False
    snapshot_currentness: str = "UNVERIFIED"
    user_approval: bool = False


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    code: str
    reason: str
    failed_requirements: tuple[GateRequirement, ...]
    requires_user_approval: bool
    recommended_action: RecommendedAction | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "failed_requirements": [item.value for item in self.failed_requirements],
            "requires_user_approval": self.requires_user_approval,
            "recommended_action": (
                self.recommended_action.to_dict() if self.recommended_action else None
            ),
        }
