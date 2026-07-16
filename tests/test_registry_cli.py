import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from project_kb.cli.app import app
from project_kb.exit_codes import (
    DESTRUCTIVE_CONFIRMATION_REQUIRED,
    GIT_REPO_ERROR,
    OK,
    REGISTRY_ERROR,
    REPO_PATH_ERROR,
    SNAPSHOT_UNAVAILABLE,
    USAGE_ERROR,
)

runner = CliRunner()


def parse_json_output(output: str) -> dict[str, object]:
    stripped = output.strip()
    assert stripped.startswith("{")
    assert stripped.endswith("}")
    return json.loads(stripped)


def fetch_project_count(registry_path: Path) -> int:
    with sqlite3.connect(registry_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM projects").fetchone()
    return int(row[0])


def test_registry_schema_tables_and_meta_are_initialized(isolated_kb_home: Path) -> None:
    result = runner.invoke(app, ["projects", "--json"])

    assert result.exit_code == OK
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        event = conn.execute(
            "SELECT event_type FROM registry_events WHERE event_type = 'schema_initialized'"
        ).fetchone()

    assert {
        "projects",
        "workspaces",
        "lifecycle_tasks",
        "lifecycle_operations",
        "snapshot_generations",
        "managed_pointers",
        "registry_events",
        "meta",
    }.issubset(tables)
    assert meta["schema_version"] == "4"
    assert "created_at" in meta
    assert "tool_version" in meta
    assert event is not None


def test_project_kb_home_override_creates_registry_only_in_test_home(
    isolated_kb_home: Path,
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["projects", "--json"])

    assert result.exit_code == OK
    payload = parse_json_output(result.output)
    assert payload["data"] == {"projects": []}
    assert (isolated_kb_home / "registry.sqlite").is_file()
    assert not (tmp_path / "localappdata" / "project-kb" / "registry.sqlite").exists()


def test_register_creates_project_record_and_storage_directories(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    result = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    assert result.exit_code == OK
    payload = parse_json_output(result.output)
    assert payload["ok"] is True
    assert payload["code"] == "PROJECT_REGISTERED"
    project = payload["data"]["project"]
    assert project["project_name"] == "repo-one"
    assert Path(project["repo_root"]).resolve() == temp_git_repo.resolve()
    assert Path(project["storage_path"]).is_dir()
    assert (Path(project["storage_path"]) / "exports").is_dir()
    assert (Path(project["storage_path"]) / "runs").is_dir()
    assert fetch_project_count(isolated_kb_home / "registry.sqlite") == 1
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        fingerprint_json = conn.execute(
            "SELECT repo_fingerprint_json FROM projects WHERE project_id = ?",
            (project["project_id"],),
        ).fetchone()[0]
    assert fingerprint_json is not None


def test_register_same_name_and_repo_is_idempotent(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    first = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    second = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    first_payload = parse_json_output(first.output)
    second_payload = parse_json_output(second.output)
    assert first.exit_code == OK
    assert second.exit_code == OK
    assert second_payload["code"] == "PROJECT_ALREADY_REGISTERED_REFRESHED"
    assert (
        second_payload["data"]["project"]["project_id"]
        == first_payload["data"]["project"]["project_id"]
    )
    assert fetch_project_count(isolated_kb_home / "registry.sqlite") == 1


def test_register_refresh_recovers_missing_expected_storage(temp_git_repo: Path) -> None:
    first = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    project = parse_json_output(first.output)["data"]["project"]
    storage_path = Path(project["storage_path"])
    (storage_path / "exports").rmdir()
    (storage_path / "runs").rmdir()
    storage_path.rmdir()

    result = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    payload = parse_json_output(result.output)
    assert result.exit_code == OK
    assert payload["code"] == "PROJECT_ALREADY_REGISTERED_REFRESHED"
    assert (storage_path / "exports").is_dir()
    assert (storage_path / "runs").is_dir()


def test_register_refresh_refuses_mismatched_storage_path(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    first = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    project = parse_json_output(first.output)["data"]["project"]
    outside_path = tmp_path / "outside-storage"
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            "UPDATE projects SET storage_path = ? WHERE project_id = ?",
            (str(outside_path), project["project_id"]),
        )

    result = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    payload = parse_json_output(result.output)
    assert result.exit_code == REGISTRY_ERROR
    assert payload["code"] == "REGISTRY_ERROR"
    assert not outside_path.exists()


def test_register_same_name_with_different_repo_blocks(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    result = runner.invoke(app, ["register", "repo-one", str(second_temp_git_repo), "--json"])

    assert result.exit_code == REGISTRY_ERROR
    payload = parse_json_output(result.output)
    assert payload["ok"] is False
    assert payload["code"] == "PROJECT_NAME_ALREADY_USED"


def test_register_same_repo_with_different_name_blocks(temp_git_repo: Path) -> None:
    runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    result = runner.invoke(app, ["register", "repo-two", str(temp_git_repo), "--json"])

    assert result.exit_code == REGISTRY_ERROR
    payload = parse_json_output(result.output)
    assert payload["code"] == "REPO_ALREADY_REGISTERED"


def test_projects_lists_registered_records(temp_git_repo: Path) -> None:
    runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])

    result = runner.invoke(app, ["projects", "--json"])

    assert result.exit_code == OK
    payload = parse_json_output(result.output)
    projects = payload["data"]["projects"]
    assert len(projects) == 1
    assert projects[0]["project_name"] == "repo-one"
    assert Path(projects[0]["repo_root"]).resolve() == temp_git_repo.resolve()


def test_relink_updates_repo_root_and_keeps_identity(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    register_result = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    registered = parse_json_output(register_result.output)["data"]["project"]

    relink_result = runner.invoke(
        app,
        ["relink", "repo-one", str(second_temp_git_repo), "--json"],
    )

    assert relink_result.exit_code == OK
    relinked = parse_json_output(relink_result.output)["data"]["project"]
    assert relinked["project_id"] == registered["project_id"]
    assert relinked["storage_path"] == registered["storage_path"]
    assert Path(relinked["repo_root"]).resolve() == second_temp_git_repo.resolve()

    status_result = runner.invoke(app, ["status", "repo-one", "--json"])
    status_payload = parse_json_output(status_result.output)
    assert status_result.exit_code == SNAPSHOT_UNAVAILABLE
    assert status_payload["data"]["repo_check"]["fingerprint_status"] == "matched"


def test_relink_to_repo_registered_by_another_project_blocks(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    runner.invoke(app, ["register", "repo-two", str(second_temp_git_repo), "--json"])

    result = runner.invoke(app, ["relink", "repo-one", str(second_temp_git_repo), "--json"])

    assert result.exit_code == REGISTRY_ERROR
    payload = parse_json_output(result.output)
    assert payload["code"] == "REPO_ALREADY_REGISTERED"


def test_unregister_without_yes_blocks_and_keeps_storage(temp_git_repo: Path) -> None:
    register_result = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    storage_path = Path(
        parse_json_output(register_result.output)["data"]["project"]["storage_path"]
    )

    result = runner.invoke(app, ["unregister", "repo-one", "--json"])

    assert result.exit_code == DESTRUCTIVE_CONFIRMATION_REQUIRED
    payload = parse_json_output(result.output)
    assert payload["code"] == "UNREGISTER_REQUIRES_YES"
    assert storage_path.is_dir()


def test_unregister_with_yes_removes_registry_record_and_storage(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    register_result = runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"])
    storage_path = Path(
        parse_json_output(register_result.output)["data"]["project"]["storage_path"]
    )

    result = runner.invoke(app, ["unregister", "repo-one", "--yes", "--json"])

    assert result.exit_code == OK
    payload = parse_json_output(result.output)
    assert payload["code"] == "PROJECT_UNREGISTERED"
    assert payload["data"]["removed_paths"]["storage_path"] == str(storage_path)
    assert not storage_path.exists()
    assert temp_git_repo.exists()
    assert fetch_project_count(isolated_kb_home / "registry.sqlite") == 0


def test_unregister_refuses_mismatched_storage_path(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    first = parse_json_output(
        runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"]).output
    )["data"]["project"]
    second = parse_json_output(
        runner.invoke(app, ["register", "repo-two", str(second_temp_git_repo), "--json"]).output
    )["data"]["project"]
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            "UPDATE projects SET storage_path = ? WHERE project_id = ?",
            (second["storage_path"], first["project_id"]),
        )

    result = runner.invoke(app, ["unregister", "repo-one", "--yes", "--json"])

    payload = parse_json_output(result.output)
    assert result.exit_code == REGISTRY_ERROR
    assert payload["code"] == "REGISTRY_ERROR"
    assert Path(second["storage_path"]).is_dir()
    assert fetch_project_count(isolated_kb_home / "registry.sqlite") == 2


def test_unregister_rejects_path_like_corrupt_project_identifier(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    first = parse_json_output(
        runner.invoke(app, ["register", "repo-one", str(temp_git_repo), "--json"]).output
    )["data"]["project"]
    second = parse_json_output(
        runner.invoke(app, ["register", "repo-two", str(second_temp_git_repo), "--json"]).output
    )["data"]["project"]
    malicious_id = f"segment/../{second['project_id']}"
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            "UPDATE projects SET project_id = ?, storage_path = ? WHERE project_id = ?",
            (malicious_id, second["storage_path"], first["project_id"]),
        )

    result = runner.invoke(app, ["unregister", "repo-one", "--yes", "--json"])

    payload = parse_json_output(result.output)
    assert result.exit_code == REGISTRY_ERROR
    assert payload["code"] == "REGISTRY_ERROR"
    assert Path(second["storage_path"]).is_dir()
    assert fetch_project_count(isolated_kb_home / "registry.sqlite") == 2


def test_invalid_project_name_blocks(temp_git_repo: Path) -> None:
    result = runner.invoke(app, ["register", "bad name", str(temp_git_repo), "--json"])

    assert result.exit_code == USAGE_ERROR
    payload = parse_json_output(result.output)
    assert payload["code"] == "INVALID_PROJECT_NAME"


def test_non_existing_repo_path_blocks(tmp_path: Path) -> None:
    result = runner.invoke(app, ["register", "repo-one", str(tmp_path / "missing"), "--json"])

    assert result.exit_code == REPO_PATH_ERROR
    payload = parse_json_output(result.output)
    assert payload["code"] == "REPO_PATH_NOT_FOUND"


def test_file_repo_path_blocks(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("not a directory", encoding="utf-8")

    result = runner.invoke(app, ["register", "repo-one", str(file_path), "--json"])

    assert result.exit_code == REPO_PATH_ERROR
    payload = parse_json_output(result.output)
    assert payload["code"] == "REPO_PATH_NOT_DIRECTORY"


def test_non_git_directory_blocks(tmp_path: Path) -> None:
    non_git_dir = tmp_path / "plain-dir"
    non_git_dir.mkdir()

    result = runner.invoke(app, ["register", "repo-one", str(non_git_dir), "--json"])

    assert result.exit_code == GIT_REPO_ERROR
    payload = parse_json_output(result.output)
    assert payload["code"] == "NOT_A_GIT_REPOSITORY"


def test_path_inside_repo_resolves_to_git_root(temp_git_repo: Path) -> None:
    nested_path = temp_git_repo / "src" / "package"
    nested_path.mkdir(parents=True)

    result = runner.invoke(app, ["register", "repo-one", str(nested_path), "--json"])

    assert result.exit_code == OK
    payload = parse_json_output(result.output)
    project = payload["data"]["project"]
    assert Path(project["repo_root"]).resolve() == temp_git_repo.resolve()
