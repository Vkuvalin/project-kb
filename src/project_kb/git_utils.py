"""Small Git helpers shared by registration and status resolution."""

import subprocess
from pathlib import Path

from project_kb.errors import (
    NotGitRepositoryError,
    RepoPathNotDirectoryError,
    RepoPathNotFoundError,
)


def resolve_git_root(repo_path: str | Path) -> Path:
    """Return the absolute top-level Git root for a path inside a repository."""

    path = Path(repo_path).expanduser()
    if not path.exists():
        raise RepoPathNotFoundError(str(repo_path))
    if not path.is_dir():
        raise RepoPathNotDirectoryError(str(repo_path))

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise NotGitRepositoryError(str(repo_path)) from exc

    git_root = result.stdout.strip()
    if not git_root:
        raise NotGitRepositoryError(str(repo_path))

    return Path(git_root).expanduser().resolve()
