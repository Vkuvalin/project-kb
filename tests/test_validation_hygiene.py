import ast
import os
from pathlib import Path
from types import SimpleNamespace

import _git_support
import pytest
from _git_support import git, sanitized_git_environment

_UNSAFE_GIT_ENVIRONMENT = {
    "GIT_DIR": "C:/sentinel/git-dir",
    "GIT_WORK_TREE": "C:/sentinel/work-tree",
    "GIT_INDEX_FILE": "C:/sentinel/index",
    "GIT_COMMON_DIR": "C:/sentinel/common",
    "GIT_OBJECT_DIRECTORY": "C:/sentinel/objects",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": "C:/sentinel/alternate-objects",
    "GIT_CONFIG": "C:/sentinel/config",
    "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "core.bare",
    "GIT_CONFIG_VALUE_0": "true",
    "GIT_CONFIG_KEY_1": "include.path",
    "GIT_CONFIG_VALUE_1": "C:/sentinel/include",
    "GIT_CEILING_DIRECTORIES": "C:/sentinel/ceiling",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
}


def test_git_child_receives_no_inherited_redirectors_or_dynamic_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _UNSAFE_GIT_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)

    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(_git_support, "subprocess", SimpleNamespace(run=fake_run))

    git(tmp_path, "status", "--short")

    child_environment = captured["env"]
    assert isinstance(child_environment, dict)
    child_keys = {key.upper() for key in child_environment}
    assert child_keys.isdisjoint(_UNSAFE_GIT_ENVIRONMENT)
    assert not {
        value for value in _UNSAFE_GIT_ENVIRONMENT.values() if value.startswith("C:/sentinel")
    }.intersection(child_environment.values())
    assert captured["cwd"] == tmp_path
    assert captured["command"] == ["git", "status", "--short"]


def test_git_environment_removes_mixed_case_controls_case_insensitively() -> None:
    parent_environment = {
        "Path": "C:/ordinary/bin",
        "TEMP": "C:/ordinary/temp",
        "git_dir": "C:/sentinel/git-dir",
        "Git_Work_Tree": "C:/sentinel/work-tree",
        "git_config_count": "1",
        "git_config_key_0": "core.bare",
        "git_config_value_0": "true",
        "Git_Trace": "1",
    }

    child_environment = sanitized_git_environment(parent_environment)

    assert child_environment["Path"] == "C:/ordinary/bin"
    assert child_environment["TEMP"] == "C:/ordinary/temp"
    assert {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_TRACE",
    }.isdisjoint(key.upper() for key in child_environment)


def test_git_uses_explicit_disposable_repository_and_leaves_redirect_target_unchanged(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a_file = temp_git_repo / "owned.txt"
    repo_b_file = second_temp_git_repo / "owned.txt"
    repo_a_file.write_text("repo A\n", encoding="utf-8")
    repo_b_file.write_text("repo B\n", encoding="utf-8")
    repo_a_index = temp_git_repo / ".git" / "index"
    repo_b_index = second_temp_git_repo / ".git" / "index"
    repo_a_index_before = repo_a_index.read_bytes()
    repo_b_index_before = repo_b_index.read_bytes()
    repo_b_status_before = git(second_temp_git_repo, "status", "--porcelain=v1", "-z").stdout

    monkeypatch.setenv("GIT_DIR", str(second_temp_git_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(second_temp_git_repo))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo_b_index))
    monkeypatch.setenv("GIT_COMMON_DIR", str(second_temp_git_repo / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(second_temp_git_repo / ".git" / "objects"))

    git(temp_git_repo, "add", "owned.txt")

    assert repo_a_index.read_bytes() != repo_a_index_before
    assert b"A  owned.txt" in git(temp_git_repo, "status", "--porcelain=v1", "-z").stdout
    assert repo_b_index.read_bytes() == repo_b_index_before
    assert git(second_temp_git_repo, "status", "--porcelain=v1", "-z").stdout == (
        repo_b_status_before
    )
    assert repo_b_file.read_text(encoding="utf-8") == "repo B\n"


def test_git_environment_preserves_ordinary_process_state_and_runs_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROJECT_KB_TEST_ORDINARY", "preserved")
    child_environment = sanitized_git_environment()
    path_key = next(key for key in child_environment if key.upper() == "PATH")

    assert child_environment[path_key] == os.environ[path_key]
    assert child_environment["PROJECT_KB_TEST_ORDINARY"] == "preserved"
    assert child_environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert child_environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child_environment["GIT_NO_LAZY_FETCH"] == "1"
    assert child_environment["GIT_OPTIONAL_LOCKS"] == "0"

    repo = tmp_path / "ordinary-environment-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")

    reported_root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.decode().strip())
    assert reported_root.resolve() == repo.resolve()


def test_test_tree_has_one_direct_git_subprocess_authority() -> None:
    test_root = Path(__file__).parent
    authority = test_root / "_git_support.py"
    bypasses: list[str] = []

    for path in test_root.rglob("*.py"):
        if path == authority:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            command = node.args[0]
            if not isinstance(command, (ast.List, ast.Tuple)) or not command.elts:
                continue
            executable = command.elts[0]
            if isinstance(executable, ast.Constant) and executable.value == "git":
                bypasses.append(f"{path.relative_to(test_root)}:{node.lineno}")

    assert bypasses == []
