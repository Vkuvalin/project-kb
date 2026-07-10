"""Central evaluation of the approved Stage 3 command requirements."""

from collections.abc import Sequence

from project_kb.gating.models import (
    GateContext,
    GateRequirement,
    GateResult,
    RecommendedAction,
)

REQUIREMENT_FIELDS = {
    GateRequirement.REGISTRY_AVAILABLE: "registry_available",
    GateRequirement.PROJECT_RESOLVED: "project_resolved",
    GateRequirement.REPO_VALID: "repo_valid",
    GateRequirement.STORAGE_VALID: "storage_valid",
    GateRequirement.SNAPSHOT_PRESENT: "snapshot_present",
    GateRequirement.SNAPSHOT_CURRENT: "snapshot_current",
    GateRequirement.USER_APPROVAL: "user_approval",
}


def evaluate_gate(
    context: GateContext,
    requirements: Sequence[GateRequirement],
    *,
    recommended_action: RecommendedAction | None = None,
) -> GateResult:
    """Evaluate an ordered requirement list without performing any action."""

    failed = tuple(
        requirement
        for requirement in requirements
        if not bool(getattr(context, REQUIREMENT_FIELDS[requirement]))
    )
    allowed = not failed
    return GateResult(
        allowed=allowed,
        code="ALLOWED" if allowed else "GATE_BLOCKED",
        reason="all_requirements_satisfied" if allowed else "requirements_not_satisfied",
        failed_requirements=failed,
        requires_user_approval=GateRequirement.USER_APPROVAL in failed,
        recommended_action=recommended_action if failed else None,
    )
