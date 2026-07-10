import json

from typer.testing import CliRunner

from project_kb.cli.app import app
from project_kb.exit_codes import OK, PROJECT_NOT_REGISTERED
from project_kb.version import __version__

runner = CliRunner()


def parse_json_output(output: str) -> dict[str, object]:
    stripped = output.strip()
    assert stripped.startswith("{")
    assert stripped.endswith("}")
    return json.loads(stripped)


def test_help_works() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == OK
    assert "version" in result.output
    assert "status" in result.output
    assert "capabilities" in result.output


def test_version_works() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == OK
    assert result.output.strip() == __version__


def test_status_json_returns_only_valid_json() -> None:
    result = runner.invoke(app, ["status", "missing", "--json"])

    assert result.exit_code == PROJECT_NOT_REGISTERED
    payload = parse_json_output(result.output)
    assert payload["ok"] is False
    assert payload["result"] == "blocked"
    assert payload["code"] == "PROJECT_NOT_REGISTERED"
    assert payload["command"] == "status"
    assert payload["data"]["project_state"] == "UNREGISTERED"
    assert payload["error"]["type"] == "ProjectStatusError"
    assert payload["meta"]["contract_version"] == 1


def test_capabilities_json_returns_only_valid_json() -> None:
    result = runner.invoke(app, ["capabilities", "--json"])

    assert result.exit_code == OK
    payload = parse_json_output(result.output)
    assert payload["ok"] is True
    assert payload["result"] == "success"
    assert payload["code"] == "OK"
    assert payload["command"] == "capabilities"
    assert payload["data"]["commands"]["version"] is True
    assert payload["data"]["commands"]["status"] is True
