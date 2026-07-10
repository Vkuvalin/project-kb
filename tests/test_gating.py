from project_kb.gating import (
    GateContext,
    GateRequirement,
    RecommendedAction,
    evaluate_gate,
)


def test_gate_reports_failed_requirements_and_user_approval() -> None:
    action = RecommendedAction(
        code="RUN_INDEX",
        command="pkb index repo-one --json",
        available=False,
        requires_user_approval=True,
        reason="snapshot_not_created",
    )

    result = evaluate_gate(
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
            GateRequirement.SNAPSHOT_PRESENT,
            GateRequirement.USER_APPROVAL,
        ),
        recommended_action=action,
    )

    assert result.allowed is False
    assert result.code == "GATE_BLOCKED"
    assert result.failed_requirements == (
        GateRequirement.SNAPSHOT_PRESENT,
        GateRequirement.USER_APPROVAL,
    )
    assert result.requires_user_approval is True
    assert result.recommended_action == action


def test_gate_allows_satisfied_project_requirements() -> None:
    result = evaluate_gate(
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

    assert result.allowed is True
    assert result.failed_requirements == ()
    assert result.recommended_action is None
