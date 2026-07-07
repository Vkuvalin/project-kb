from project_kb.errors import ProjectKbError
from project_kb.exit_codes import USAGE_ERROR
from project_kb.output.json import build_error_response


def test_project_kb_error_serializes_into_error_envelope() -> None:
    error = ProjectKbError(
        code="USAGE_ERROR",
        message="Invalid command input.",
        exit_code=USAGE_ERROR,
        recommended_action="Check command help.",
        retryable=False,
        details={"argument": "--example"},
    )

    envelope = build_error_response(error, command="example")

    assert envelope["ok"] is False
    assert envelope["code"] == "USAGE_ERROR"
    assert envelope["error"] == {
        "type": "ProjectKbError",
        "code": "USAGE_ERROR",
        "message": "Invalid command input.",
        "recommended_action": "Check command help.",
        "retryable": False,
        "details": {"argument": "--example"},
    }
