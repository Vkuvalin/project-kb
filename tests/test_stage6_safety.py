import contextlib
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from _stage6_support import git

from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.resolver.project import ProjectStatusService

_UNSAFE_GIT_CONTROLS = {
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


@dataclass(frozen=True)
class _DisposableGitStorageSnapshot:
    index_path: Path
    git_dir: Path
    common_dir: Path
    files: tuple[tuple[str, bytes], ...]

    @property
    def shared_indexes(self) -> tuple[tuple[str, bytes], ...]:
        return tuple(item for item in self.files if Path(item[0]).name.startswith("sharedindex."))


def _resolved_git_path(repo_root: Path, *arguments: str) -> Path:
    from project_kb.git_utils import git_text

    raw = git_text(repo_root, "rev-parse", *arguments)
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve(strict=True)


def _snapshot_disposable_git_storage(repo_root: Path) -> _DisposableGitStorageSnapshot:
    """Capture bytes and paths only inside one disposable repository's Git storage."""

    index_path = _resolved_git_path(repo_root, "--git-path", "index")
    git_dir = index_path.parent
    common_dir = _resolved_git_path(repo_root, "--git-common-dir")
    files: dict[str, bytes] = {}
    for root in {git_dir, common_dir, index_path.parent}:
        for path in root.rglob("*"):
            if path.is_file():
                files[os.path.normcase(os.path.abspath(path))] = path.read_bytes()
    return _DisposableGitStorageSnapshot(
        index_path=index_path,
        git_dir=git_dir,
        common_dir=common_dir,
        files=tuple(sorted(files.items())),
    )


def _managed_temporary_indexes(storage_path: str) -> tuple[Path, ...]:
    return tuple(sorted(Path(storage_path).glob("project-kb-git-index-*")))


def _index_tracked_module(repo_root: Path, *, project_name: str = "repo-one"):
    module = repo_root / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(repo_root, "add", "module.py")
    git(repo_root, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    project = RegistryService().register(project_name, repo_root).project
    assert project is not None
    IndexService().index(project_name)
    return project


def _assert_current_without_git_storage_writes(
    repo_root: Path,
    *,
    project_name: str,
    storage_path: str,
    mode: str,
) -> None:
    before = _snapshot_disposable_git_storage(repo_root)
    assert _managed_temporary_indexes(storage_path) == ()

    status = ProjectStatusService().status(project_name, verification_mode=mode)

    assert status.snapshot_check.currentness == "CURRENT"
    assert _snapshot_disposable_git_storage(repo_root) == before
    assert _managed_temporary_indexes(storage_path) == ()


def test_canonical_git_runner_sets_complete_read_only_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb import git_utils

    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(*args: object, **kwargs: object) -> Result:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    git_utils.run_git(
        tmp_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        check=False,
    )

    environment = captured["kwargs"]["env"]
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    command = captured["args"][0]
    assert command[:6] == [
        "git",
        "--no-pager",
        "-c",
        "core.quotepath=false",
        "-c",
        "core.fsmonitor=false",
    ]


def test_safe_git_environment_removes_parent_redirectors_and_dynamic_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.git_utils import safe_git_environment

    for key, value in _UNSAFE_GIT_CONTROLS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("git_dir", "C:/sentinel/lowercase-git-dir")
    monkeypatch.setenv("PATH", "C:/normal/path")
    monkeypatch.setenv("TEMP", "C:/normal/temp")

    environment = safe_git_environment()

    assert environment["PATH"] == "C:/normal/path"
    assert environment["TEMP"] == "C:/normal/temp"
    assert not any(
        key.upper().startswith("GIT_")
        for key in environment
        if key
        not in {
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_NO_LAZY_FETCH",
            "GIT_OPTIONAL_LOCKS",
        }
    )
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"


def test_registry_scanner_and_currentness_ignore_parent_git_redirectors(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    for key, value in _UNSAFE_GIT_CONTROLS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("git_dir", "C:/sentinel/lowercase-git-dir")

    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"


def test_remote_identity_config_read_is_local_and_does_not_follow_includes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb import git_utils

    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(*args: object, **kwargs: object) -> Result:
        captured["command"] = args[0]
        return Result()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)

    git_utils.run_git(
        tmp_path,
        "config",
        "--local",
        "--no-includes",
        "--get",
        "remote.origin.url",
    )

    assert captured["command"][-6:] == [
        "core.fsmonitor=false",
        "config",
        "--local",
        "--no-includes",
        "--get",
        "remote.origin.url",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("status", "--porcelain=v1", "--output=target-owned.txt"),
        ("config", "--get", "core.fsmonitor"),
        ("cat-file", "-e", "--filters"),
        ("diff", "--output=target-owned.txt"),
        ("update-index", "-z", "--index-info"),
    ],
)
def test_canonical_git_runner_rejects_unapproved_argument_shapes(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    from project_kb.git_utils import run_git

    with pytest.raises(ValueError, match="outside the read-only observation boundary"):
        run_git(tmp_path, *arguments)

    assert not (tmp_path / "target-owned.txt").exists()


def test_temporary_index_visibility_normalization_is_isolated_and_cleaned(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    git(temp_git_repo, "update-index", "--assume-unchanged", "module.py")
    from project_kb.git_utils import temporary_git_index
    from project_kb.indexing.scanner import git_index_flagged_paths

    live_index = temp_git_repo / ".git" / "index"
    live_bytes = live_index.read_bytes()
    live_flags = git(temp_git_repo, "ls-files", "-v", "-z").stdout

    with temporary_git_index(temp_git_repo, temporary_root=tmp_path) as index_view:
        temporary_path = index_view.path
        assert git_index_flagged_paths(temp_git_repo, index_view=index_view) == ()
        assert live_index.read_bytes() == live_bytes
        assert git(temp_git_repo, "ls-files", "-v", "-z").stdout == live_flags

    assert temporary_path.exists() is False
    assert live_index.read_bytes() == live_bytes


def test_temporary_index_refuses_a_root_inside_the_source_repository(
    temp_git_repo: Path,
) -> None:
    from project_kb.git_utils import GitIndexChangedError, temporary_git_index

    before = tuple(temp_git_repo.iterdir())
    with (
        pytest.raises(GitIndexChangedError, match="outside the safe boundary"),
        temporary_git_index(temp_git_repo, temporary_root=temp_git_repo),
    ):
        raise AssertionError("unsafe temporary index root was accepted")

    assert tuple(temp_git_repo.iterdir()) == before


def test_visibility_neutral_observation_does_not_write_split_index_storage(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    git(temp_git_repo, "update-index", "--split-index")
    from project_kb.indexing.scanner import capture_repo_observation

    git_dir = temp_git_repo / ".git"
    index_paths = (git_dir / "index", *sorted(git_dir.glob("sharedindex.*")))
    assert len(index_paths) > 1
    before = {path: path.read_bytes() for path in index_paths}

    observation = capture_repo_observation(temp_git_repo, temporary_root=tmp_path)

    assert observation.sealed is True
    assert observation.state.visibility_sealed is True
    assert {path: path.read_bytes() for path in index_paths} == before
    assert tuple(sorted(git_dir.glob("sharedindex.*"))) == index_paths[1:]


def test_temporary_index_write_disables_split_index_for_its_command(
    temp_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _index_tracked_module(temp_git_repo)
    git(temp_git_repo, "config", "core.splitIndex", "true")
    from project_kb import git_utils
    from project_kb.indexing.scanner import capture_repo_observation

    real_run = git_utils.subprocess.run
    temporary_write_commands: list[list[str]] = []

    def recording_run(*args: object, **kwargs: object):
        command = args[0]
        if isinstance(command, list) and "update-index" in command:
            temporary_write_commands.append(command)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_utils.subprocess, "run", recording_run)

    observation = capture_repo_observation(temp_git_repo, temporary_root=tmp_path)

    assert observation.sealed is True
    assert len(temporary_write_commands) == 1
    command = temporary_write_commands[0]
    update_index_position = command.index("update-index")
    assert command[update_index_position - 2 : update_index_position] == [
        "-c",
        "core.splitIndex=false",
    ]
    assert _managed_temporary_indexes(project.storage_path) == ()


@pytest.mark.parametrize("mode", ["strong"])
def test_persistent_split_index_config_cannot_write_live_git_storage(
    temp_git_repo: Path,
    mode: str,
) -> None:
    project = _index_tracked_module(temp_git_repo)
    git(temp_git_repo, "config", "core.splitIndex", "true")
    before = _snapshot_disposable_git_storage(temp_git_repo)
    assert before.shared_indexes == ()

    _assert_current_without_git_storage_writes(
        temp_git_repo,
        project_name="repo-one",
        storage_path=project.storage_path,
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["strong"])
def test_existing_split_index_is_byte_identical_after_verification(
    temp_git_repo: Path,
    mode: str,
) -> None:
    project = _index_tracked_module(temp_git_repo)
    git(temp_git_repo, "config", "core.splitIndex", "true")
    git(temp_git_repo, "update-index", "--split-index")
    before = _snapshot_disposable_git_storage(temp_git_repo)
    assert before.shared_indexes

    _assert_current_without_git_storage_writes(
        temp_git_repo,
        project_name="repo-one",
        storage_path=project.storage_path,
        mode=mode,
    )


def test_construction_exception_cleans_temp_and_preserves_live_git_storage(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _index_tracked_module(temp_git_repo)
    git(temp_git_repo, "config", "core.splitIndex", "true")
    before = _snapshot_disposable_git_storage(temp_git_repo)
    from project_kb import git_utils

    real_run_git = git_utils.run_git

    def fail_after_temporary_write(repo_root: Path, *args: str, **kwargs: object):
        result = real_run_git(repo_root, *args, **kwargs)
        if args == ("update-index", "-z", "--index-info"):
            raise subprocess.CalledProcessError(
                1,
                result.args,
                output=result.stdout,
                stderr=b"injected post-write failure",
            )
        return result

    monkeypatch.setattr(git_utils, "run_git", fail_after_temporary_write)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness != "CURRENT"
    assert status.snapshot_check.currentness == "CHANGED_DURING_CHECK"
    assert _snapshot_disposable_git_storage(temp_git_repo) == before
    assert _managed_temporary_indexes(project.storage_path) == ()


def test_observation_exception_cleans_temp_and_preserves_live_git_storage(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _index_tracked_module(temp_git_repo)
    git(temp_git_repo, "config", "core.splitIndex", "true")
    before = _snapshot_disposable_git_storage(temp_git_repo)
    from project_kb.indexing import scanner

    real_git_candidates = scanner.git_candidates

    def fail_after_construction(
        repo_root: Path,
        policy=None,
        *,
        index_view=None,
    ):
        if index_view is not None:
            raise OSError("injected observation failure")
        return real_git_candidates(repo_root, policy, index_view=index_view)

    monkeypatch.setattr(scanner, "git_candidates", fail_after_construction)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "ERROR"
    assert _snapshot_disposable_git_storage(temp_git_repo) == before
    assert _managed_temporary_indexes(project.storage_path) == ()


def test_cleanup_failure_is_reported_and_does_not_write_live_git_storage(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _index_tracked_module(temp_git_repo)
    git(temp_git_repo, "config", "core.splitIndex", "true")
    before = _snapshot_disposable_git_storage(temp_git_repo)
    from project_kb import git_utils

    real_temporary_directory = git_utils.tempfile.TemporaryDirectory

    class CleanupReportingFailure:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._inner = real_temporary_directory(*args, **kwargs)

        def __enter__(self) -> str:
            return self._inner.__enter__()

        def __exit__(self, *args: object) -> bool:
            self._inner.__exit__(*args)
            raise OSError("injected cleanup reporting failure")

    monkeypatch.setattr(git_utils.tempfile, "TemporaryDirectory", CleanupReportingFailure)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "ERROR"
    assert _snapshot_disposable_git_storage(temp_git_repo) == before
    assert _managed_temporary_indexes(project.storage_path) == ()


@pytest.mark.parametrize("mode", ["strong"])
def test_linked_worktree_verification_preserves_worktree_and_common_git_storage(
    temp_git_repo: Path,
    mode: str,
) -> None:
    linked_root = temp_git_repo.parent / f"linked-{mode}"
    try:
        git(temp_git_repo, "worktree", "add", "--detach", str(linked_root))
    except subprocess.CalledProcessError as exc:
        reason = exc.stderr.decode("utf-8", errors="replace").strip()
        pytest.skip(f"linked worktree fixture is unavailable: {reason}")
    project_name = f"linked-{mode}"
    project = _index_tracked_module(linked_root, project_name=project_name)
    git(linked_root, "config", "core.splitIndex", "true")
    git(linked_root, "update-index", "--split-index")
    before = _snapshot_disposable_git_storage(linked_root)
    assert before.git_dir != before.common_dir
    assert before.shared_indexes

    _assert_current_without_git_storage_writes(
        linked_root,
        project_name=project_name,
        storage_path=project.storage_path,
        mode=mode,
    )


def test_strong_currentness_never_opens_or_hashes_hard_secret_content(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = temp_git_repo / ".env"
    secret.write_text("TOKEN=stage6-secret\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        proof = connection.execute(
            "SELECT proof_class, content_hash FROM proof_manifest WHERE relative_path = ?",
            (".env",),
        ).fetchone()
    secret.write_text("TOKEN=stage6-secrex\n", encoding="utf-8")

    from project_kb.indexing import scanner as scanner_module

    real_open = scanner_module.os.open
    real_path_open = Path.open

    def guarded_open(path: object, *args: object, **kwargs: object):
        if Path(os.fspath(path)).name == ".env":
            raise AssertionError("hard-secret content was opened")
        return real_open(path, *args, **kwargs)

    def guarded_path_open(path: Path, *args: object, **kwargs: object):
        if path.name == ".env":
            raise AssertionError("hard-secret content was opened through pathlib")
        return real_path_open(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert proof == ("EXCLUDED_HARD_SECRET", None)
    assert status.snapshot_check.currentness == "CURRENT"
    assert ".env" in status.snapshot_check.exclusions


def test_index_and_strong_verification_never_execute_target_module(temp_git_repo: Path) -> None:
    sentinel = temp_git_repo / "target-executed.txt"
    (temp_git_repo / "trap.py").write_text(
        "from pathlib import Path\nPath('target-executed.txt').write_text('bad')\n",
        encoding="utf-8",
    )
    RegistryService().register("repo-one", temp_git_repo)

    IndexService().index("repo-one")
    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"
    assert not sentinel.exists()
