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
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo_path
