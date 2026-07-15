"""Bounded read-only Git execution shared by all repository observations."""

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from project_kb.errors import (
    NotGitRepositoryError,
    RepoPathNotDirectoryError,
    RepoPathNotFoundError,
)

_ALLOWED_GIT_ARGUMENTS: Final = frozenset(
    {
        ("config", "--local", "--no-includes", "--get", "remote.origin.url"),
        ("ls-files", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("ls-files", "--stage", "-z"),
        ("ls-files", "-v", "-z"),
        ("rev-list", "--max-parents=0", "--all"),
        ("rev-parse", "--git-common-dir"),
        ("rev-parse", "--git-path", "index"),
        ("rev-parse", "--is-shallow-repository"),
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--verify", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
    }
)
_TEMP_INDEX_WRITE_ARGUMENTS: Final = frozenset(
    {
        ("update-index", "-z", "--index-info"),
    }
)
_COMMIT_OBJECT_ARGUMENT: Final = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\^\{commit\}\Z")
_GIT_ENVIRONMENT_PREFIX: Final = "GIT_"
_TEMP_INDEX_CAPABILITY: Final = object()


class GitIndexChangedError(RuntimeError):
    """The live Git index could not be observed as one stable generation."""


@dataclass(frozen=True)
class TemporaryGitIndex:
    """Capability for one isolated visibility-neutral live index generation."""

    path: Path
    source_path: Path
    source_generation: str
    _capability: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class _GitIndexSnapshot:
    path: Path
    generation: str


def safe_git_environment() -> dict[str, str]:
    """Return a normal process environment without inherited Git controls.

    The allow-listed commands must observe the explicit ``repo_root`` rather than a
    parent-selected directory, index, object store, namespace, or injected config.
    No inherited ``GIT_*`` variable is retained; the four approved read-only values
    below are the complete Git-specific environment for a child process. Ordinary
    process variables such as ``PATH`` and ``TEMP`` remain available for Git on
    Windows.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(_GIT_ENVIRONMENT_PREFIX)
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def run_git(
    repo_root: Path,
    *args: str,
    check: bool = True,
    index_view: TemporaryGitIndex | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded Git command without hooks, live-index writes, or lazy fetches."""

    temp_index_write = args in _TEMP_INDEX_WRITE_ARGUMENTS
    if not _allowed_git_arguments(args, temporary_index=index_view is not None):
        command = " ".join(args)
        raise ValueError(f"Git arguments are outside the read-only observation boundary: {command}")
    if input_bytes is not None and not temp_index_write:
        raise ValueError("Git stdin is allowed only for bounded temporary-index construction")
    if temp_index_write and input_bytes is None:
        raise ValueError("Temporary-index construction requires explicit staged-entry input")
    environment = safe_git_environment()
    if index_view is not None:
        if index_view._capability is not _TEMP_INDEX_CAPABILITY:
            raise ValueError("Git index view was not created by the bounded temporary-index owner")
        environment["GIT_INDEX_FILE"] = str(index_view.path)
    command_config = [
        "-c",
        "core.quotepath=false",
        "-c",
        "core.fsmonitor=false",
    ]
    if temp_index_write:
        command_config.extend(["-c", "core.splitIndex=false"])
    return subprocess.run(
        [
            "git",
            "--no-pager",
            *command_config,
            *args,
        ],
        cwd=repo_root,
        check=check,
        capture_output=True,
        env=environment,
        input=input_bytes,
    )


def _allowed_git_arguments(args: tuple[str, ...], *, temporary_index: bool) -> bool:
    if args in _ALLOWED_GIT_ARGUMENTS:
        return True
    if temporary_index and args in _TEMP_INDEX_WRITE_ARGUMENTS:
        return True
    return (
        len(args) == 3
        and args[:2] == ("cat-file", "-e")
        and _COMMIT_OBJECT_ARGUMENT.fullmatch(args[2]) is not None
    )


def git_bytes(
    repo_root: Path,
    *args: str,
    check: bool = True,
    index_view: TemporaryGitIndex | None = None,
) -> bytes:
    return run_git(repo_root, *args, check=check, index_view=index_view).stdout


def git_text(
    repo_root: Path,
    *args: str,
    allow_failure: bool = False,
    index_view: TemporaryGitIndex | None = None,
) -> str:
    process = run_git(
        repo_root,
        *args,
        check=not allow_failure,
        index_view=index_view,
    )
    if process.returncode != 0:
        return ""
    return process.stdout.decode("utf-8", errors="strict").strip()


def git_z(
    repo_root: Path,
    *args: str,
    index_view: TemporaryGitIndex | None = None,
) -> list[str]:
    payload = git_bytes(repo_root, *args, index_view=index_view)
    return [item.decode("utf-8", errors="surrogateescape") for item in payload.split(b"\0") if item]


@contextmanager
def temporary_git_index(
    repo_root: Path,
    *,
    temporary_root: Path,
) -> Iterator[TemporaryGitIndex]:
    """Yield an isolated index rebuilt from one sealed live staged-entry view.

    Rebuilding with ``--index-info`` deliberately omits assume-unchanged,
    skip-worktree, split-index, sparse-index, and fsmonitor extensions. The
    temporary view therefore preserves staged entries while making worktree
    visibility neutral. Its write command also disables ``core.splitIndex`` so
    Git cannot place ``sharedindex.*`` in the live common directory.
    """

    source = _read_live_git_index(repo_root)
    staged_entries = git_bytes(repo_root, "ls-files", "--stage", "-z")
    visibility_entries = git_bytes(repo_root, "ls-files", "-v", "-z")
    source_generation = _combined_index_generation(
        source.generation,
        staged_entries,
        visibility_entries,
    )
    try:
        if temporary_root.is_symlink() or temporary_root.is_junction():
            raise GitIndexChangedError("temporary Git index root is redirected")
        managed_root = temporary_root.resolve(strict=True)
        source_root = repo_root.resolve(strict=True)
    except GitIndexChangedError:
        raise
    except OSError as exc:
        raise GitIndexChangedError("temporary Git index root could not be resolved") from exc
    if (
        not managed_root.is_dir()
        or managed_root == source_root
        or managed_root.is_relative_to(source_root)
    ):
        raise GitIndexChangedError("temporary Git index root is outside the safe boundary")
    with tempfile.TemporaryDirectory(
        prefix="project-kb-git-index-",
        dir=managed_root,
    ) as directory:
        temporary_path = (Path(directory) / "index").absolute()
        index_view = TemporaryGitIndex(
            path=temporary_path,
            source_path=source.path,
            source_generation=source_generation,
            _capability=_TEMP_INDEX_CAPABILITY,
        )
        try:
            run_git(
                repo_root,
                "update-index",
                "-z",
                "--index-info",
                index_view=index_view,
                input_bytes=staged_entries,
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            raise GitIndexChangedError("temporary Git index could not be created") from exc
        if not temporary_path.is_file():
            raise GitIndexChangedError("temporary Git index was not created as a regular file")
        yield index_view


def live_git_index_generation(repo_root: Path, *, expected_path: Path | None = None) -> str:
    """Return generation evidence for the current live index without modifying it."""

    snapshot = _read_live_git_index(repo_root)
    staged_entries = git_bytes(repo_root, "ls-files", "--stage", "-z")
    visibility_entries = git_bytes(repo_root, "ls-files", "-v", "-z")
    generation = _combined_index_generation(
        snapshot.generation,
        staged_entries,
        visibility_entries,
    )
    if expected_path is not None and _normalized_path(snapshot.path) != _normalized_path(
        expected_path
    ):
        return hashlib.sha256(
            f"INDEX_PATH_CHANGED\0{_normalized_path(snapshot.path)}\0{generation}".encode(
                "utf-8",
                errors="surrogateescape",
            )
        ).hexdigest()
    return generation


def _combined_index_generation(
    raw_generation: str,
    staged_entries: bytes,
    visibility_entries: bytes,
) -> str:
    """Seal raw identity with logical staged-entry and visibility projections."""

    return hashlib.sha256(
        raw_generation.encode("ascii")
        + b"\0"
        + hashlib.sha256(staged_entries).digest()
        + b"\0"
        + hashlib.sha256(visibility_entries).digest()
    ).hexdigest()


def _read_live_git_index(repo_root: Path) -> _GitIndexSnapshot:
    raw_path = git_text(repo_root, "rev-parse", "--git-path", "index")
    if not raw_path:
        raise GitIndexChangedError("Git did not report a live index path")
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path
    path = path.absolute()
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or path.is_symlink() or path.is_junction():
            raise GitIndexChangedError("live Git index is not a direct regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_before = os.fstat(descriptor)
        current_before_read = path.stat(follow_symlinks=False)
        if (
            not _same_index_object(before, opened_before)
            or _index_stat_signature(before) != _index_stat_signature(current_before_read)
            or path.is_symlink()
            or path.is_junction()
        ):
            raise GitIndexChangedError("live Git index identity changed before read")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read()
            opened_after = os.fstat(handle.fileno())
        after = path.stat(follow_symlinks=False)
        if path.is_symlink() or path.is_junction():
            raise GitIndexChangedError("live Git index became redirected during read")
    except GitIndexChangedError:
        raise
    except OSError as exc:
        raise GitIndexChangedError("live Git index could not be observed") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    before_signature = _index_stat_signature(before)
    after_signature = _index_stat_signature(after)
    if (
        not _same_index_object(before, opened_before)
        or not _same_index_object(before, opened_after)
        or before_signature != after_signature
        or len(payload) != after.st_size
    ):
        raise GitIndexChangedError("live Git index changed during generation capture")
    content_hash = hashlib.sha256(payload).hexdigest()
    generation_payload = "\0".join(
        [_normalized_path(path), *(str(value) for value in after_signature), content_hash]
    )
    return _GitIndexSnapshot(
        path=path,
        generation=hashlib.sha256(
            generation_payload.encode("utf-8", errors="surrogateescape")
        ).hexdigest(),
    )


def _index_stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_dev,
        value.st_ino,
    )


def _same_index_object(expected: os.stat_result, actual: os.stat_result) -> bool:
    if not stat.S_ISREG(actual.st_mode):
        return False
    if expected.st_dev and actual.st_dev and expected.st_dev != actual.st_dev:
        return False
    if expected.st_ino and actual.st_ino and expected.st_ino != actual.st_ino:
        return False
    return expected.st_size == actual.st_size and expected.st_mtime_ns == actual.st_mtime_ns


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def resolve_git_root(repo_path: str | Path) -> Path:
    """Return the absolute top-level Git root for a path inside a repository."""

    path = Path(repo_path).expanduser()
    if not path.exists():
        raise RepoPathNotFoundError(str(repo_path))
    if not path.is_dir():
        raise RepoPathNotDirectoryError(str(repo_path))

    try:
        git_root = git_text(path, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise NotGitRepositoryError(str(repo_path)) from exc

    if not git_root:
        raise NotGitRepositoryError(str(repo_path))

    return Path(git_root).expanduser().resolve()
