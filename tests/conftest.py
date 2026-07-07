from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_project_kb_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project_kb_home = tmp_path / "project-kb-home"
    local_app_data = tmp_path / "localappdata"
    monkeypatch.setenv("PROJECT_KB_HOME", str(project_kb_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    return project_kb_home
