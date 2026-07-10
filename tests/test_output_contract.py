from project_kb.output.json import REQUIRED_ENVELOPE_FIELDS, build_response


def test_json_envelope_contains_required_top_level_fields() -> None:
    envelope = build_response(
        ok=True,
        result="success",
        code="OK",
        command="test",
        message="Test response.",
        data={"value": 1},
    )

    assert tuple(envelope) == REQUIRED_ENVELOPE_FIELDS
    assert envelope["warnings"] == []
    assert envelope["error"] is None
    assert envelope["meta"]["tool"] == "project-kb"
    assert envelope["meta"]["contract_version"] == 1


def test_json_envelope_uses_structured_warning_objects() -> None:
    warning = {
        "code": "TEST_WARNING",
        "message": "This is a structured warning.",
        "details": {"field": "value"},
    }

    envelope = build_response(
        ok=True,
        result="success",
        code="OK",
        command="test",
        message="Test response.",
        warnings=[warning],
    )

    assert envelope["warnings"] == [warning]
