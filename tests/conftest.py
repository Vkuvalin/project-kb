import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_kb_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project_kb_home = tmp_path / "project-kb-home"
    local_app_data = tmp_path / "localappdata"
    monkeypatch.setenv("PROJECT_KB_HOME", str(project_kb_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    return project_kb_home


@pytest.fixture
def isolated_project_kb_home(isolated_kb_home: Path) -> Path:
    return isolated_kb_home


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    return _init_git_repo(tmp_path / "repo-one")


@pytest.fixture
def second_temp_git_repo(tmp_path: Path) -> Path:
    return _init_git_repo(tmp_path / "repo-two")


def _init_git_repo(repo_path: Path) -> Path:
    repo_path.mkdir()
    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Project KB Tests",
            "GIT_AUTHOR_EMAIL": "project-kb-tests@example.invalid",
            "GIT_COMMITTER_NAME": "Project KB Tests",
            "GIT_COMMITTER_EMAIL": "project-kb-tests@example.invalid",
        }
    )
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    (repo_path / "README.md").write_text(f"# {repo_path.name}\n", encoding="utf-8")
    hooks_path = repo_path / ".git" / "disabled-hooks"
    hooks_path.mkdir()
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={hooks_path}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Initial test commit",
        ],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    return repo_path
