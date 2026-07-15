"""Sanitized Git execution for disposable test repositories."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


def sanitized_git_environment(
    parent_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Preserve ordinary process state while replacing all inherited Git controls."""

    parent = os.environ if parent_environment is None else parent_environment
    environment = {
        key: value for key, value in parent.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": "Project KB Tests",
            "GIT_AUTHOR_EMAIL": "project-kb-tests@example.invalid",
            "GIT_COMMITTER_NAME": "Project KB Tests",
            "GIT_COMMITTER_EMAIL": "project-kb-tests@example.invalid",
        }
    )
    return environment


def git(
    repo: Path,
    *args: str,
    text: bool = False,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    """Run Git against one explicit disposable repository with no inherited controls."""

    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=text,
        env=sanitized_git_environment(),
    )
