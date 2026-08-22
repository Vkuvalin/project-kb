import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from _git_support import git as run_test_git
from typer.testing import CliRunner

from project_kb.cli.app import app
from project_kb.exit_codes import (
    GIT_REPO_ERROR,
    PROJECT_NOT_REGISTERED,
    REGISTRY_ERROR,
    REPO_PATH_ERROR,
    REPOSITORY_MISMATCH,
    SNAPSHOT_UNAVAILABLE,
    STORAGE_STATE_ERROR,
    USAGE_ERROR,
)
from project_kb.registry.service import normalize_repo_root
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.state import STATE_POLICIES, ProjectState

runner = CliRunner()
REQUIRED_STATUS_SECTIONS = {
    "snapshot_source",
    "resolution",
    "project",
    "project_state",
    "repo_check",
    "storage_check",
    "snapshot_check",
    "availability",
    "requires_user_action",
    "recommended_action",
}


def test_status_resolves_explicit_project_name_and_returns_complete_contract(
    temp_git_repo: Path,
) -> None:
    project = _register("Repo-One", temp_git_repo)

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    assert result.exit_code == SNAPSHOT_UNAVAILABLE
    payload = _parse_json(result.output)
    policy = STATE_POLICIES[ProjectState.REGISTERED_NO_SNAPSHOT]
    assert payload["ok"] is policy.ok
    assert payload["result"] == policy.result
    assert payload["code"] == "REGISTERED_NO_SNAPSHOT"
    assert payload["error"] is None
    assert payload["meta"]["contract_version"] == 1
    assert set(payload["data"]) == REQUIRED_STATUS_SECTIONS
    assert payload["data"]["snapshot_source"] == "LEGACY_CANONICAL"
    assert payload["data"]["resolution"]["resolved_by"] == "project_name"
    assert payload["data"]["project"]["project_id"] == project["project_id"]
    assert {
        "project_id",
        "project_name",
        "repo_root",
        "storage_path",
        "created_at",
        "updated_at",
        "last_indexed_at",
        "last_git_commit",
    }.issubset(payload["data"]["project"])
    assert payload["data"]["project_state"] == "REGISTERED_NO_SNAPSHOT"
    assert payload["data"]["repo_check"]["status"] == "valid"
    assert payload["data"]["repo_check"]["fingerprint_status"] == "matched"
    assert payload["data"]["storage_check"]["status"] == "valid"
    assert payload["data"]["snapshot_check"] == {
        "status": "absent",
        "snapshot_id": None,
        "indexed_at": None,
        "git_commit_at_index": None,
        "current_git_commit": None,
        "is_current": False,
        "reason": "snapshot_not_created",
        "availability": "MISSING",
        "compatibility": "NOT_APPLICABLE",
        "currentness": "UNVERIFIED",
        "truth_claim": None,
        "verification_mode": None,
        "verified_at": None,
        "verification_duration_ms": None,
        "verification_timings_ms": {},
        "verification_attempts": 0,
        "mismatch_paths": [],
        "deltas": [],
        "diagnostics": [],
        "exclusions": [],
        "verification_scope": {},
        "proof_contract_version": None,
        "verifier_version": None,
    }
    assert payload["data"]["availability"] == {
        "can_use_project": True,
        "can_use_snapshot": False,
        "can_search": False,
        "can_generate_exports": False,
        "can_generate_context": False,
    }
    assert payload["data"]["requires_user_action"] is True
    assert payload["data"]["recommended_action"] == {
        "code": "RUN_INDEX",
        "command": "pkb index Repo-One --json",
        "available": False,
        "requires_user_approval": True,
        "reason": "snapshot_not_created",
    }


def test_status_resolves_registered_project_from_current_directory(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _register("repo-one", temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == SNAPSHOT_UNAVAILABLE
    payload = _parse_json(result.output)
    assert payload["data"]["project"]["project_id"] == project["project_id"]
    assert payload["data"]["resolution"]["mode"] == "current_directory"
    assert payload["data"]["resolution"]["resolved_by"] == "repo_root"


def test_status_resolves_nested_directory_to_registered_git_root(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register("repo-one", temp_git_repo)
    nested = temp_git_repo / "src" / "package"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["status", "--json"])

    payload = _parse_json(result.output)
    assert result.exit_code == SNAPSHOT_UNAVAILABLE
    assert Path(payload["data"]["resolution"]["resolved_repo_root"]) == temp_git_repo.resolve()


def test_unknown_name_is_unregistered_without_creating_registry(
    isolated_kb_home: Path,
) -> None:
    result = runner.invoke(app, ["status", "unknown", "--json"])

    payload = _assert_problem(result, PROJECT_NOT_REGISTERED, "PROJECT_NOT_REGISTERED")
    assert payload["data"]["project_state"] == "UNREGISTERED"
    assert payload["data"]["project"] is None
    assert payload["data"]["recommended_action"]["command"] == (
        "pkb register unknown <repo_path> --json"
    )
    assert not isolated_kb_home.exists()


def test_problem_outcome_uses_state_policy_runtime_values(
    isolated_kb_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProjectState.UNREGISTERED
    custom_policy = replace(
        STATE_POLICIES[state],
        ok=True,
        result="policy-controlled-result",
        exit_code=91,
        has_error=False,
        requires_user_action=False,
    )
    monkeypatch.setitem(STATE_POLICIES, state, custom_policy)

    outcome = ProjectStatusService(home=isolated_kb_home).status("unknown")

    assert outcome.ok is custom_policy.ok
    assert outcome.result == custom_policy.result
    assert outcome.exit_code == custom_policy.exit_code
    assert outcome.error is None
    assert outcome.requires_user_action is custom_policy.requires_user_action


def test_invalid_status_name_returns_safe_structured_action() -> None:
    result = runner.invoke(app, ["status", "bad name", "--json"])

    payload = _assert_problem(result, USAGE_ERROR, "INVALID_PROJECT_NAME")
    action = payload["data"]["recommended_action"]
    assert action["code"] == "CHOOSE_VALID_PROJECT_NAME"
    assert action["command"] is None


def test_current_directory_outside_git_repo_returns_specific_error(
    isolated_kb_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain_directory = tmp_path / "plain"
    plain_directory.mkdir()
    monkeypatch.chdir(plain_directory)

    result = runner.invoke(app, ["status", "--json"])

    payload = _assert_problem(
        result,
        GIT_REPO_ERROR,
        "CURRENT_DIRECTORY_NOT_GIT_REPO",
    )
    assert payload["data"]["project_state"] == "UNREGISTERED"
    assert payload["data"]["resolution"]["status"] == "not_git_repository"
    assert not isolated_kb_home.exists()


def test_unregistered_git_repo_returns_unregistered(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(temp_git_repo)

    result = runner.invoke(app, ["status", "--json"])

    payload = _assert_problem(result, PROJECT_NOT_REGISTERED, "PROJECT_NOT_REGISTERED")
    assert payload["data"]["resolution"]["resolved_repo_root"] == str(temp_git_repo.resolve())
    command = payload["data"]["recommended_action"]["command"]
    assert command.startswith("pkb register <name> ")
    assert str(temp_git_repo.resolve()) in command
    assert command.endswith(" --json")
    assert "<repo_path>" not in command
    assert not isolated_kb_home.exists()


def test_recommended_action_quotes_known_windows_path_with_spaces(
    temp_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_with_spaces = tmp_path / "repo with spaces"
    temp_git_repo.rename(repo_with_spaces)
    monkeypatch.chdir(repo_with_spaces)

    result = runner.invoke(app, ["status", "--json"])

    payload = _assert_problem(result, PROJECT_NOT_REGISTERED, "PROJECT_NOT_REGISTERED")
    command = payload["data"]["recommended_action"]["command"]
    assert command == f"pkb register <name> '{repo_with_spaces.resolve()}' --json"
    assert "<repo_path>" not in command


def test_missing_registered_repo_path_is_broken(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    _register("repo-one", temp_git_repo)
    temp_git_repo.rename(tmp_path / "moved-repo")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, REPO_PATH_ERROR, "BROKEN_PATH")
    assert payload["data"]["project_state"] == "BROKEN_PATH"
    assert payload["data"]["repo_check"]["path_exists"] is False


def test_registered_repo_path_that_is_a_file_is_broken(temp_git_repo: Path) -> None:
    _register("repo-one", temp_git_repo)
    temp_git_repo.rename(temp_git_repo.with_name("original-repo"))
    temp_git_repo.write_text("not a directory", encoding="utf-8")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, REPO_PATH_ERROR, "BROKEN_PATH")
    assert payload["data"]["repo_check"]["is_directory"] is False


def test_registered_path_without_git_metadata_is_not_git_repository(
    temp_git_repo: Path,
) -> None:
    _register("repo-one", temp_git_repo)
    (temp_git_repo / ".git").rename(temp_git_repo / ".git-disabled")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, GIT_REPO_ERROR, "NOT_A_GIT_REPOSITORY")
    assert payload["data"]["project_state"] == "NOT_A_GIT_REPOSITORY"


def test_git_root_mismatch_is_classified(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    _register("repo-one", temp_git_repo)
    nested = temp_git_repo / "nested"
    nested.mkdir()
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            """UPDATE workspaces
               SET workspace_root = ?, workspace_root_norm = ?,
                   row_version = row_version + 1, updated_at = updated_at
               WHERE project_id = (
                   SELECT project_id FROM projects WHERE project_name_norm = ?
               ) AND workspace_kind = 'PRIMARY'""",
            (str(nested), normalize_repo_root(nested), "repo-one"),
        )

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, REPOSITORY_MISMATCH, "REPO_MISMATCH")
    assert payload["data"]["repo_check"]["git_root_matches"] is False


def test_status_initializes_legacy_fingerprint_once(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    _register("repo-one", temp_git_repo)
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            """UPDATE workspaces
               SET repository_fingerprint_json = NULL,
                   row_version = row_version + 1, updated_at = updated_at
               WHERE project_id = (
                   SELECT project_id FROM projects WHERE project_name_norm = ?
               ) AND workspace_kind = 'PRIMARY'""",
            ("repo-one",),
        )

    first = runner.invoke(app, ["status", "repo-one", "--json"])
    second = runner.invoke(app, ["status", "repo-one", "--json"])

    first_payload = _parse_json(first.output)
    second_payload = _parse_json(second.output)
    assert first_payload["data"]["repo_check"]["fingerprint_status"] == "initialized"
    assert second_payload["data"]["repo_check"]["fingerprint_status"] == "matched"
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        fingerprint_json = conn.execute(
            "SELECT repo_fingerprint_json FROM projects WHERE project_name_norm = 'repo-one'"
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM registry_events WHERE event_type = 'repo_fingerprint_initialized'"
        ).fetchone()[0]
    assert fingerprint_json is not None
    assert event_count == 1


def test_replacement_repository_at_same_path_is_fingerprint_mismatch(
    temp_git_repo: Path,
) -> None:
    _register("repo-one", temp_git_repo)
    (temp_git_repo / ".git").rename(temp_git_repo / ".git-original")
    (temp_git_repo / "README.md").write_text("replacement identity\n", encoding="utf-8")
    _initialize_existing_repo(temp_git_repo, "Replacement root")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, REPOSITORY_MISMATCH, "REPO_MISMATCH")
    assert payload["data"]["repo_check"]["fingerprint_status"] == "mismatch"
    assert payload["data"]["repo_check"]["reason"] == "root_commits_changed"


def test_normal_new_commit_does_not_change_repository_identity(temp_git_repo: Path) -> None:
    _register("repo-one", temp_git_repo)
    (temp_git_repo / "new-file.txt").write_text("new content\n", encoding="utf-8")
    _run_git(temp_git_repo, "add", "new-file.txt")
    _run_git(temp_git_repo, "commit", "-m", "Normal follow-up commit")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _parse_json(result.output)
    assert result.exit_code == SNAPSHOT_UNAVAILABLE
    assert payload["data"]["repo_check"]["fingerprint_status"] == "matched"


def test_empty_git_repository_remains_usable_with_unavailable_fingerprint(
    tmp_path: Path,
) -> None:
    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    _run_git(empty_repo, "init")
    _register("empty-repo", empty_repo)

    result = runner.invoke(app, ["status", "empty-repo", "--json"])

    payload = _parse_json(result.output)
    assert result.exit_code == SNAPSHOT_UNAVAILABLE
    assert payload["data"]["project_state"] == "REGISTERED_NO_SNAPSHOT"
    assert payload["data"]["repo_check"]["fingerprint_status"] == "unavailable"
    assert payload["data"]["repo_check"]["fingerprint_strength"] == "unavailable"


@pytest.mark.parametrize("missing_part", ["storage", "exports", "runs"])
def test_missing_storage_is_reported_without_silent_repair(
    temp_git_repo: Path,
    missing_part: str,
) -> None:
    project = _register("repo-one", temp_git_repo)
    storage_path = Path(project["storage_path"])
    missing_path = storage_path if missing_part == "storage" else storage_path / missing_part
    if missing_path == storage_path:
        shutil.rmtree(missing_path)
    else:
        missing_path.rmdir()

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, STORAGE_STATE_ERROR, "STORAGE_MISSING")
    assert payload["data"]["project_state"] == "STORAGE_MISSING"
    assert str(missing_path) in payload["data"]["storage_check"]["missing_paths"]
    assert not missing_path.exists()
    action_command = payload["data"]["recommended_action"]["command"]
    assert action_command.startswith("pkb register repo-one ")
    assert str(temp_git_repo.resolve()) in action_command
    assert action_command.endswith(" --json")
    assert "<name>" not in action_command
    assert "<repo_path>" not in action_command


def test_storage_path_outside_expected_home_is_mismatch_without_probe_or_repair(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    _register("repo-one", temp_git_repo)
    outside_path = tmp_path / "outside-storage"
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            "UPDATE projects SET storage_path = ? WHERE project_name_norm = ?",
            (str(outside_path), "repo-one"),
        )

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, STORAGE_STATE_ERROR, "STORAGE_MISMATCH")
    check = payload["data"]["storage_check"]
    assert check["actual_storage_path"] == str(outside_path.resolve())
    assert check["storage_exists"] is None
    assert not outside_path.exists()


def test_redirected_exports_directory_is_not_valid_storage(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    project = _register("repo-one", temp_git_repo)
    exports_path = Path(project["storage_path"]) / "exports"
    exports_path.rmdir()
    outside_exports = tmp_path / "outside-exports"
    outside_exports.mkdir()
    try:
        exports_path.symlink_to(outside_exports, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable in this Windows environment.")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, STORAGE_STATE_ERROR, "STORAGE_MISSING")
    assert payload["data"]["storage_check"]["exports_exists"] is False
    assert payload["data"]["availability"]["can_use_project"] is False
    assert outside_exports.is_dir()

    refresh = runner.invoke(
        app,
        ["register", "repo-one", str(temp_git_repo), "--json"],
    )
    refresh_payload = _parse_json(refresh.output)
    assert refresh.exit_code == REGISTRY_ERROR
    assert refresh_payload["code"] == "REGISTRY_ERROR"
    assert exports_path.is_symlink() or exports_path.is_junction()


def test_invalid_registry_returns_complete_structured_status_error(
    isolated_kb_home: Path,
) -> None:
    isolated_kb_home.mkdir(parents=True)
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    payload = _assert_problem(result, REGISTRY_ERROR, "REGISTRY_ERROR")
    policy = STATE_POLICIES[ProjectState.REGISTRY_ERROR]
    assert payload["ok"] is policy.ok
    assert payload["result"] == policy.result
    assert (payload["error"] is not None) is policy.has_error
    assert payload["data"]["requires_user_action"] is policy.requires_user_action
    assert payload["data"]["project_state"] == "REGISTRY_ERROR"
    assert set(payload["data"]) == REQUIRED_STATUS_SECTIONS


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/DEVELOPMENT_CHECKLIST.md",
        "docs/DATA_SAFETY_POLICY.md",
    ],
)
def test_durable_docs_use_current_project_wording(relative_path: str) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    content = (repository_root / relative_path).read_text(encoding="utf-8")

    assert "Stage 3" not in content
    assert "Command Center" not in content


def test_data_safety_policy_rejects_unsafe_path_redirects() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    content = (repository_root / "docs/DATA_SAFETY_POLICY.md").read_text(encoding="utf-8")

    assert "symlinks or junctions" in content
    assert "rejected rather than followed" in content


def test_raw_remote_origin_is_never_exposed_or_persisted(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    private_remote = "ssh://private-user@private.example.invalid/secret/repo.git"
    _run_git(temp_git_repo, "remote", "add", "origin", private_remote)
    _register("repo-one", temp_git_repo)

    result = runner.invoke(app, ["status", "repo-one", "--json"])

    assert private_remote not in result.output
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        fingerprint_json = conn.execute(
            "SELECT repo_fingerprint_json FROM projects WHERE project_name_norm = 'repo-one'"
        ).fetchone()[0]
        event_details = "\n".join(
            row[0] or "" for row in conn.execute("SELECT details_json FROM registry_events")
        )
    assert private_remote not in fingerprint_json
    assert private_remote not in event_details
    assert json.loads(fingerprint_json)["remote_origin_hash"] is not None


def _register(name: str, repo_root: Path) -> dict[str, object]:
    result = runner.invoke(app, ["register", name, str(repo_root), "--json"])
    assert result.exit_code == 0, result.output
    return _parse_json(result.output)["data"]["project"]


def _assert_problem(result: object, exit_code: int, code: str) -> dict[str, object]:
    assert result.exit_code == exit_code
    payload = _parse_json(result.output)
    assert payload["ok"] is False
    assert payload["result"] == "blocked"
    assert payload["code"] == code
    assert payload["error"]["code"] == code
    assert set(payload["data"]) == REQUIRED_STATUS_SECTIONS
    assert set(payload["data"]["recommended_action"]) == {
        "code",
        "command",
        "available",
        "requires_user_approval",
        "reason",
    }
    return payload


def _parse_json(output: str) -> dict[str, object]:
    stripped = output.strip()
    assert stripped.startswith("{")
    assert stripped.endswith("}")
    return json.loads(stripped)


def _initialize_existing_repo(repo_root: Path, message: str) -> None:
    _run_git(repo_root, "init")
    hooks_path = repo_root / ".git" / "disabled-hooks"
    hooks_path.mkdir(exist_ok=True)
    _run_git(repo_root, "add", "README.md")
    _run_git(
        repo_root,
        "-c",
        f"core.hooksPath={hooks_path}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
    )


def _run_git(repo_root: Path, *args: str) -> None:
    run_test_git(repo_root, *args, text=True)
