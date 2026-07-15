import ast
import contextlib
import io
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from _git_support import git as run_test_git
from typer.testing import CliRunner

from project_kb.cli.app import app
from project_kb.errors import IndexingError, RegistryOperationError, SnapshotQueryError
from project_kb.indexing.extractor import extract_python
from project_kb.indexing.models import Candidate, ScanPolicy
from project_kb.indexing.scanner import (
    FileAccessError,
    FileReadError,
    RepositoryChangedError,
    ScanError,
    UnsafePathError,
    _read_bounded_stream,
    _read_safe_file,
    git_candidates,
)
from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.snapshot.database import SnapshotReader, validate_snapshot

runner = CliRunner()


def test_universal_ignored_runtime_population_is_bounded_and_visible(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / ".gitignore").write_text("data/\n.venv/\n", encoding="utf-8")
    (temp_git_repo / "data" / "photos").mkdir(parents=True)
    (temp_git_repo / "data" / "results").mkdir()
    (temp_git_repo / ".venv").mkdir()
    (temp_git_repo / "data" / "app.db").write_bytes(b"sqlite-runtime-data")
    for index in range(100):
        (temp_git_repo / "data" / "photos" / f"image-{index}.bin").write_bytes(b"x")
    (temp_git_repo / "data" / "results" / "result.json").write_text("{}")
    (temp_git_repo / ".venv" / "ignored.py").write_text("VALUE = 1\n")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None

    outcome = IndexService().index("repo-one")

    assert outcome.data["counts"]["candidates"] < 20
    database = Path(project.storage_path) / "kb.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as conn:
        runtime_db = conn.execute(
            """SELECT analysis_level, classification_reason, content_hash
               FROM files WHERE relative_path = 'data/app.db'"""
        ).fetchone()
        roots = {row[0] for row in conn.execute("SELECT relative_path FROM pruned_roots")}
        deep_files = conn.execute(
            """SELECT COUNT(*) FROM files
               WHERE relative_path LIKE 'data/photos/%'
                  OR relative_path LIKE 'data/results/%'
                  OR relative_path LIKE '.venv/%'"""
        ).fetchone()[0]
    assert runtime_db is None
    assert roots == {".venv"}
    assert deep_files == 0


def test_universal_defaults_have_no_reference_repository_paths() -> None:
    policy = ScanPolicy()

    assert policy.discovered_metadata_paths == ()
    assert "data/photos" not in policy.discovered_pruned_roots
    assert "data/results" not in policy.discovered_pruned_roots
    assert policy == ScanPolicy()


def test_production_index_service_uses_only_canonical_universal_policy() -> None:
    first = IndexService()
    second = IndexService()

    assert first.policy == second.policy == ScanPolicy()
    with pytest.raises(TypeError):
        IndexService(policy=ScanPolicy(policy_version="custom"))  # type: ignore[call-arg]


def test_index_status_and_query_share_canonical_policy_version(temp_git_repo: Path) -> None:
    (temp_git_repo / "module.py").write_text("def value():\n    return 1\n")
    RegistryService().register("repo-one", temp_git_repo)

    outcome = IndexService().index("repo-one")
    status = IndexService().status_service.status("repo-one")
    query = runner.invoke(app, ["symbols", "repo-one", "--file", "module.py", "--json"])
    query_policy = json.loads(query.output)["data"]["snapshot"]["policy_version"]

    assert query.exit_code == 0
    assert outcome.data["snapshot"]["policy_version"] == ScanPolicy().policy_version
    assert query_policy == ScanPolicy().policy_version
    assert status.availability.can_use_snapshot is True


def test_tracked_reference_named_path_remains_git_visible(temp_git_repo: Path) -> None:
    contract = temp_git_repo / "data" / "results" / "contract.json"
    contract.parent.mkdir(parents=True)
    contract.write_text('{"contract": true}\n', encoding="utf-8")
    _git(temp_git_repo, "add", "data/results/contract.json")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None

    IndexService().index("repo-one")

    with contextlib.closing(sqlite3.connect(Path(project.storage_path) / "kb.sqlite")) as conn:
        row = conn.execute(
            """SELECT relative_path, git_population, analysis_level, content_hash
               FROM files WHERE relative_path = 'data/results/contract.json'"""
        ).fetchone()
    assert row is not None
    assert row[:3] == ("data/results/contract.json", "TRACKED", "TEXT_STRUCTURAL")
    assert row[3]


def test_git_path_spelling_wins_over_policy_discovery(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    monkeypatch.setattr(
        scanner,
        "_git_z",
        lambda _root, *args: ["Mixed/Case.py"] if args == ("ls-files", "-z") else [],
    )
    monkeypatch.setattr(
        scanner,
        "_policy_candidates",
        lambda _root, _policy: [Candidate("mixed/case.py", "POLICY_DISCOVERED")],
    )

    assert git_candidates(temp_git_repo) == [Candidate("Mixed/Case.py", "TRACKED")]


def test_precise_secret_policy_keeps_source_indexable_and_never_reads_secrets(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_names = [
        "token_service.py",
        "token_validator.py",
        "credential_service.py",
        "credential_validator.py",
        "client_secret_model.py",
    ]
    for name in source_names:
        (temp_git_repo / name).write_text(f"def {name[:-3]}():\n    return True\n")
    secret_names = [
        "secrets.yaml",
        ".env",
        ".env.local",
        ".env.production",
        "private.key",
        "token.json",
        "tokens.json",
        "client_secret.json",
        "client_secrets.json",
        "service-account.json",
        "service_account.json",
    ]
    template_names = [".env.example", ".env.sample", ".env.template", ".env.dist"]
    for name in secret_names:
        (temp_git_repo / name).write_text("DO_NOT_READ=1\n")
    for name in template_names:
        (temp_git_repo / name).write_text("SAFE_TEMPLATE=1\n")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    from project_kb.indexing import scanner

    original_open = scanner.os.open

    def guarded_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path).name in secret_names:
            raise AssertionError("hard-secret content was opened")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(scanner.os, "open", guarded_open)
    IndexService().index("repo-one")

    with contextlib.closing(sqlite3.connect(Path(project.storage_path) / "kb.sqlite")) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT relative_path, analysis_level, file_kind, content_hash FROM files"
            )
        }
    for name in source_names:
        assert rows[name][0] == "TEXT_STRUCTURAL"
        assert rows[name][2] is not None
    for name in template_names:
        assert rows[name][0] == "TEXT_STRUCTURAL"
        assert rows[name][2] is not None
    for name in secret_names:
        assert rows[name] == ("METADATA_ONLY", "HARD_SECRET", None)


def test_binary_probe_stops_before_reading_remaining_content() -> None:
    class TrackingStream(io.BytesIO):
        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return super().read(size)

    stream = TrackingStream(b"\0" + b"x" * 100_000)

    data, binary = _read_bounded_stream(stream, max_bytes=200_000, probe_bytes=32)

    assert binary is True
    assert data is None
    assert stream.read_sizes == [32]


def test_safe_read_detects_candidate_replacement_between_lstat_and_open(
    temp_git_repo: Path,
) -> None:
    path = temp_git_repo / "replace.py"
    path.write_text("VALUE = 1\n")
    expected = path.lstat()
    replacement = temp_git_repo / "replacement.tmp"
    replacement.write_text("VALUE = 2\n")
    os.replace(replacement, path)

    with pytest.raises(ScanError, match="identity changed"):
        _read_safe_file(temp_git_repo, path, expected, ScanPolicy())


def test_safe_read_blocks_static_redirected_parent(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    parent = temp_git_repo / "parent"
    parent.mkdir()
    path = parent / "module.py"
    path.write_text("VALUE = 1\n")
    real_is_redirect = scanner._is_redirect
    monkeypatch.setattr(
        scanner,
        "_is_redirect",
        lambda candidate: candidate == parent or real_is_redirect(candidate),
    )

    with pytest.raises(ScanError, match="redirected parent"):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_safe_read_detects_parent_replacement_immediately_before_open(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    parent = temp_git_repo / "parent"
    parent.mkdir()
    path = parent / "module.py"
    path.write_text("VALUE = 1\n")
    real_validate = scanner._validated_parent_components
    calls = 0

    def changing_parent(*args: object):
        nonlocal calls
        calls += 1
        evidence = real_validate(*args)
        if calls == 2:
            part, signature = evidence[0]
            return ((part, (*signature[:-1], signature[-1] + 1)),)
        return evidence

    monkeypatch.setattr(scanner, "_validated_parent_components", changing_parent)

    with pytest.raises(RepositoryChangedError, match="parent identity changed"):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_safe_read_detects_parent_replacement_during_read(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    parent = temp_git_repo / "parent"
    parent.mkdir()
    path = parent / "module.py"
    path.write_text("VALUE = 1\n")
    calls = 0

    def verify_parent(*_args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RepositoryChangedError("candidate parent identity changed during read")

    monkeypatch.setattr(scanner, "_verify_parent_components", verify_parent)

    with pytest.raises(RepositoryChangedError, match="during read"):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_redirect_introduced_after_initial_observation_is_repository_change(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    path = temp_git_repo / "module.py"
    path.write_text("VALUE = 1\n")
    redirect_checks = 0

    def redirect_after_initial(candidate: Path) -> bool:
        nonlocal redirect_checks
        if candidate == path:
            redirect_checks += 1
            return redirect_checks > 1
        return False

    monkeypatch.setattr(scanner, "_is_redirect", redirect_after_initial)

    with pytest.raises(RepositoryChangedError, match="identity changed"):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_candidate_disappearing_after_initial_observation_is_repository_change(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    path = temp_git_repo / "module.py"
    path.write_text("VALUE = 1\n")
    monkeypatch.setattr(scanner, "_lstat_for_read", lambda _path: None)

    with pytest.raises(RepositoryChangedError, match="identity changed"):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_static_unsafe_path_is_not_repository_change(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n")

    with pytest.raises(UnsafePathError):
        _read_safe_file(temp_git_repo, outside, outside.lstat(), ScanPolicy())


def test_permission_error_is_file_access_not_repository_change(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    path = temp_git_repo / "module.py"
    path.write_text("VALUE = 1\n")

    def permission_denied(*_args: object, **_kwargs: object) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(scanner.os, "open", permission_denied)

    with pytest.raises(FileAccessError):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_ordinary_read_error_is_not_repository_change(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.indexing import scanner

    path = temp_git_repo / "module.py"
    path.write_text("VALUE = 1\n")

    def failed_read(*_args: object, **_kwargs: object):
        raise OSError("ordinary read failure")

    monkeypatch.setattr(scanner, "_read_bounded_stream", failed_read)

    with pytest.raises(FileReadError):
        _read_safe_file(temp_git_repo, path, path.lstat(), ScanPolicy())


def test_dirty_text_change_with_unchanged_porcelain_blocks_publication(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = temp_git_repo / "README.md"
    target.write_text("dirty-0\n")
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_scan = service_module.scan_repository
    calls = 0

    def changing_scan(*args: object, **kwargs: object):
        nonlocal calls
        facts = real_scan(*args, **kwargs)
        calls += 1
        target.write_text(f"dirty-{calls}\n")
        return facts

    monkeypatch.setattr(service_module, "scan_repository", changing_scan)
    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")
    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert calls == 2


def test_metadata_only_change_with_unchanged_porcelain_blocks_publication(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = temp_git_repo / "asset.bin"
    target.write_bytes(b"asset-0")
    _git(temp_git_repo, "add", "asset.bin")
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_scan = service_module.scan_repository
    calls = 0

    def changing_scan(*args: object, **kwargs: object):
        nonlocal calls
        facts = real_scan(*args, **kwargs)
        calls += 1
        target.write_bytes(f"asset-{calls}".encode())
        return facts

    monkeypatch.setattr(service_module, "scan_repository", changing_scan)
    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")
    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert calls == 2


def test_identity_change_during_read_retries_once_and_then_publishes(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_scan = service_module.scan_repository
    calls = 0

    def first_read_changes(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RepositoryChangedError("identity changed during read")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(service_module, "scan_repository", first_read_changes)

    outcome = IndexService().index("repo-one")

    assert outcome.result in {"success", "success_with_warnings"}
    assert calls == 2


@pytest.mark.parametrize("reason", ["redirect introduced", "candidate disappeared"])
def test_observed_repository_mutation_retries_once(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_scan = service_module.scan_repository
    calls = 0

    def first_attempt_changes(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RepositoryChangedError(reason)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(service_module, "scan_repository", first_attempt_changes)

    IndexService().index("repo-one")

    assert calls == 2


@pytest.mark.parametrize("error_type", [UnsafePathError, FileAccessError, FileReadError])
def test_non_mutation_scan_failure_does_not_retry_or_report_repository_changed(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[ScanError],
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    calls = 0

    def fail_without_mutation(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        raise error_type("static or ordinary failure")

    monkeypatch.setattr(service_module, "scan_repository", fail_without_mutation)

    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code != "REPO_CHANGED_DURING_SCAN"
    assert calls == 1


def test_second_identity_change_returns_repository_changed(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    calls = 0

    def always_changes(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        raise RepositoryChangedError("identity changed during read")

    monkeypatch.setattr(service_module, "scan_repository", always_changes)

    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert calls == 2


@pytest.mark.parametrize("phase", ["write_snapshot", "validate_snapshot"])
def test_change_during_snapshot_build_or_validation_blocks_publication(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    target = temp_git_repo / "README.md"
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_phase = getattr(service_module, phase)
    calls = 0

    def mutate_after_phase(*args: object, **kwargs: object):
        nonlocal calls
        result = real_phase(*args, **kwargs)
        calls += 1
        target.write_text(f"changed-{calls}\n")
        return result

    monkeypatch.setattr(service_module, phase, mutate_after_phase)

    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert calls == 2


def test_final_verification_failure_blocks_atomic_replace(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    verification_calls = 0

    def unstable_final_verification(*_args: object, **_kwargs: object) -> bool:
        nonlocal verification_calls
        verification_calls += 1
        return verification_calls % 2 == 1

    def forbidden_publish(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("os.replace publication must not be reached")

    monkeypatch.setattr(service_module, "verify_scan_evidence", unstable_final_verification)
    monkeypatch.setattr(service_module, "publish_snapshot", forbidden_publish)

    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert verification_calls == 4


def test_inheritance_relations_use_each_base_expression_range() -> None:
    source = """class Single(Base):
    value = 1
    value_2 = 2

class Multi(
    First,
    package.Second[
        Item
    ],
):
    value = 1
    value_2 = 2
    value_3 = 3
"""
    tree = ast.parse(source)
    expected = [base for node in tree.body if isinstance(node, ast.ClassDef) for base in node.bases]

    _, _, relations, diagnostics = extract_python(
        file_id="file", relative_path="module.py", source=source, line_count=13
    )
    actual = [item for item in relations if item.relation_kind == "CLASS_INHERITS_EXPRESSION"]

    assert not diagnostics
    assert len(actual) == len(expected) == 3
    for relation, base in zip(actual, expected, strict=True):
        assert (
            relation.start_line,
            relation.end_line,
            relation.start_column,
            relation.end_column,
        ) == (base.lineno, base.end_lineno, base.col_offset, base.end_col_offset)
    assert actual[0].end_line == 1


def test_temporary_and_canonical_snapshot_keep_sealed_build_state(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_write = service_module.write_snapshot
    temporary_states: list[tuple[str, str]] = []

    def inspect_temporary(*args: object, **kwargs: object):
        result = real_write(*args, **kwargs)
        database = args[0]
        with contextlib.closing(sqlite3.connect(database)) as conn:
            temporary_states.append(
                (
                    conn.execute("SELECT build_status FROM snapshot_meta").fetchone()[0],
                    conn.execute("SELECT status FROM index_runs").fetchone()[0],
                )
            )
        return result

    monkeypatch.setattr(service_module, "write_snapshot", inspect_temporary)
    outcome = IndexService().index("repo-one")
    database = Path(outcome.data["project"]["storage_path"]) / "kb.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as conn:
        canonical = (
            conn.execute("SELECT build_status FROM snapshot_meta").fetchone()[0],
            conn.execute("SELECT status FROM index_runs").fetchone()[0],
        )

    assert temporary_states == [("SEALED", "BUILD_SUCCEEDED")]
    assert canonical == ("SEALED", "BUILD_SUCCEEDED")


def test_canonical_snapshot_bytes_equal_sealed_temp_after_replace(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    from project_kb.indexing import service as service_module

    real_publish = service_module.publish_snapshot
    sealed_bytes: list[bytes] = []

    def capture_atomic_replace(temp_path: Path, current_path: Path) -> bool:
        sealed_bytes.append(temp_path.read_bytes())
        return real_publish(temp_path, current_path)

    monkeypatch.setattr(service_module, "publish_snapshot", capture_atomic_replace)
    IndexService().index("repo-one")
    canonical = Path(project.storage_path) / "kb.sqlite"

    assert canonical.read_bytes() == sealed_bytes[0]
    validate_snapshot(canonical, project_id=project.project_id)


def test_publish_snapshot_does_not_reopen_canonical_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_kb.snapshot import database as database_module

    temp_path = tmp_path / "sealed.sqlite"
    current_path = tmp_path / "kb.sqlite"
    temp_path.write_bytes(b"sealed snapshot bytes")

    def forbidden_connect(*_args: object, **_kwargs: object):
        raise AssertionError("canonical SQLite must not be reopened after os.replace")

    monkeypatch.setattr(database_module.sqlite3, "connect", forbidden_connect)

    assert database_module.publish_snapshot(temp_path, current_path) is False
    assert current_path.read_bytes() == b"sealed snapshot bytes"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_table",
        "missing_column",
        "missing_index",
        "missing_run",
        "foreign_run_id",
        "project_id_mismatch",
        "published_snapshot_id_mismatch",
        "multiple_publication_runs",
    ],
)
def test_schema_manifest_and_published_run_invariants_are_validated(
    temp_git_repo: Path,
    mutation: str,
) -> None:
    project, database = _indexed_project(temp_git_repo)
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        snapshot_id = conn.execute("SELECT snapshot_id FROM snapshot_meta").fetchone()[0]
        if mutation == "missing_table":
            conn.execute("DROP TABLE relations")
        elif mutation == "missing_column":
            conn.execute("ALTER TABLE relations DROP COLUMN start_column")
        elif mutation == "missing_index":
            conn.execute("DROP INDEX symbols_short_name_idx")
        elif mutation == "missing_run":
            conn.execute("DELETE FROM index_runs")
        elif mutation == "foreign_run_id":
            run = list(conn.execute("SELECT * FROM index_runs").fetchone())
            run[0] = "foreign-run"
            run[-2] = "foreign-snapshot"
            conn.execute(
                f"INSERT INTO index_runs VALUES ({','.join('?' for _ in run)})",
                run,
            )
            conn.execute("UPDATE snapshot_meta SET run_id = 'foreign-run'")
        elif mutation == "project_id_mismatch":
            conn.execute("UPDATE index_runs SET project_id = 'foreign-project'")
        elif mutation == "published_snapshot_id_mismatch":
            conn.execute("UPDATE index_runs SET published_snapshot_id = ?", ("f" * 32,))
        else:
            run = list(conn.execute("SELECT * FROM index_runs").fetchone())
            run[0] = "second-run"
            conn.execute(
                f"INSERT INTO index_runs VALUES ({','.join('?' for _ in run)})",
                run,
            )
        conn.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)
    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"
    if mutation == "published_snapshot_id_mismatch":
        assert snapshot_id != "f" * 32


def test_snapshot_meta_binds_to_exactly_one_matching_run(temp_git_repo: Path) -> None:
    project, database = _indexed_project(temp_git_repo)

    meta = validate_snapshot(database, project_id=project.project_id)

    with contextlib.closing(sqlite3.connect(database)) as conn:
        run = conn.execute(
            "SELECT run_id, project_id, published_snapshot_id, status FROM index_runs"
        ).fetchone()
    assert run == (
        meta["run_id"],
        meta["project_id"],
        meta["snapshot_id"],
        "BUILD_SUCCEEDED",
    )


@pytest.mark.parametrize(
    ("column", "value", "compatibility_key"),
    [
        ("schema_version", 999, "schema_compatible"),
        ("scanner_version", "old-scanner", "scanner_compatible"),
        ("policy_version", "old-policy", "policy_compatible"),
        ("extractor_versions_json", '{"python-ast": "old"}', "extractor_compatible"),
    ],
)
def test_snapshot_semantic_mismatch_requires_rebuild(
    temp_git_repo: Path,
    column: str,
    value: object,
    compatibility_key: str,
) -> None:
    project, database = _indexed_project(temp_git_repo)
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.execute(f"UPDATE snapshot_meta SET {column} = ?", (value,))
        conn.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)
    assert captured.value.code == "SNAPSHOT_REBUILD_REQUIRED"
    assert captured.value.details["compatibility"][compatibility_key] is False

    status = runner.invoke(app, ["status", "repo-one", "--json"])
    assert status.exit_code == 21
    assert json.loads(status.output)["data"]["project_state"] == "SNAPSHOT_REBUILD_REQUIRED"


def test_incompatible_snapshot_blocks_queries_and_full_rebuild_recovers(
    temp_git_repo: Path,
) -> None:
    project, database = _indexed_project(temp_git_repo)
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.execute("UPDATE snapshot_meta SET policy_version = 'old-policy'")
        conn.commit()

    query = runner.invoke(app, ["symbols", "repo-one", "--json"])
    query_payload = json.loads(query.output)
    assert query.exit_code == 21
    assert query_payload["code"] == "SNAPSHOT_REBUILD_REQUIRED"
    assert query_payload["error"]["details"]["compatibility"]["policy_compatible"] is False

    rebuild = runner.invoke(app, ["index", "repo-one", "--full", "--json"])
    assert rebuild.exit_code == 0, rebuild.output
    validate_snapshot(database, project_id=project.project_id)


def test_registry_failure_after_publication_reconciles_to_published_snapshot(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = temp_git_repo / "module.py"
    source.write_text("def before():\n    return 1\n")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.indexing import service as service_module

    real_write = service_module.write_snapshot

    def fail_build(*args: object, **kwargs: object):
        raise IndexingError("induced refresh failure")

    monkeypatch.setattr(service_module, "write_snapshot", fail_build)
    with pytest.raises(IndexingError):
        IndexService().index("repo-one")
    monkeypatch.setattr(service_module, "write_snapshot", real_write)
    source.write_text("def after():\n    return 2\n")
    service = IndexService()

    def fail_registry(*args: object, **kwargs: object):
        raise RegistryOperationError("induced registry failure")

    monkeypatch.setattr(service.registry, "record_index_outcome", fail_registry)
    outcome = service.index("repo-one")

    assert outcome.result == "success_with_warnings"
    assert outcome.data["publication"]["state"] == "PUBLISHED"
    assert outcome.data["publication"]["bookkeeping"] == {
        "registry": "not_recorded",
        "run_file": "recorded",
    }
    assert any(
        warning["code"] == "REGISTRY_INDEX_STATUS_NOT_RECORDED" for warning in outcome.warnings
    )
    status = runner.invoke(app, ["status", "repo-one", "--json"])
    status_payload = json.loads(status.output)
    assert status_payload["data"]["project_state"] == "SNAPSHOT_PRESENT_REGISTRY_WARNING"
    assert status_payload["warnings"][0]["code"] == "PUBLISHED_SNAPSHOT_REGISTRY_OUTCOME_STALE"
    query = runner.invoke(app, ["symbols", "repo-one", "--file", "module.py", "--json"])
    query_payload = json.loads(query.output)
    assert query.exit_code == 0
    assert query_payload["result"] == "success_with_warnings"
    assert any(item["short_name"] == "after" for item in query_payload["data"]["symbols"])


def test_same_root_relink_after_replace_quarantines_published_snapshot(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    service = IndexService()
    real_record_outcome = service.registry.record_index_outcome

    def relink_before_recording(*args: object, **kwargs: object):
        RegistryService().relink("repo-one", temp_git_repo)
        return real_record_outcome(*args, **kwargs)

    monkeypatch.setattr(service.registry, "record_index_outcome", relink_before_recording)

    with pytest.raises(IndexingError) as captured:
        service.index("repo-one")

    assert captured.value.code == "SNAPSHOT_REBUILD_REQUIRED"
    assert captured.value.details["published"] is True
    assert captured.value.details["usable"] is False
    assert captured.value.details["publication_state"] == "PUBLISHED_QUARANTINED"
    active = RegistryService().find_project_by_name("repo-one")
    assert active is not None
    assert active.repo_binding_generation != project.repo_binding_generation
    database = Path(project.storage_path) / "kb.sqlite"
    metadata = validate_snapshot(database, project_id=project.project_id)
    assert metadata["repository_binding_generation"] == project.repo_binding_generation
    status = service.status_service.status("repo-one")
    assert status.project_state == "SNAPSHOT_REBUILD_REQUIRED"
    assert status.availability.can_use_snapshot is False
    query = runner.invoke(app, ["symbols", "repo-one", "--json"])
    assert query.exit_code == 21
    assert not list(Path(project.storage_path).glob("*.bak"))


def test_unexpected_exception_after_replace_never_records_index_failed(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("def published():\n    return True\n")
    RegistryService().register("repo-one", temp_git_repo)
    service = IndexService()

    def unexpected_registry_failure(*_args: object, **_kwargs: object) -> None:
        raise ValueError("unexpected ancillary failure")

    monkeypatch.setattr(service.registry, "record_index_outcome", unexpected_registry_failure)
    outcome = service.index("repo-one")

    assert outcome.result == "success_with_warnings"
    assert outcome.data["publication"]["state"] == "PUBLISHED"
    assert outcome.data["publication"]["bookkeeping"] == {
        "registry": "unknown",
        "run_file": "recorded",
    }
    assert any(warning["details"]["error_type"] == "ValueError" for warning in outcome.warnings)
    status = service.status_service.status("repo-one")
    assert not (status.project and (status.project.last_status or "").startswith("INDEX_FAILED:"))
    query = runner.invoke(app, ["symbols", "repo-one", "--file", "module.py", "--json"])
    assert query.exit_code == 0


def test_exception_raised_after_successful_replace_reconciles_to_published(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("def published():\n    return True\n")
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_publish = service_module.publish_snapshot

    def publish_then_raise(*args: object, **kwargs: object) -> bool:
        real_publish(*args, **kwargs)
        raise RuntimeError("exception after canonical replace")

    monkeypatch.setattr(service_module, "publish_snapshot", publish_then_raise)
    service = IndexService()
    outcome = service.index("repo-one")

    assert outcome.result == "success_with_warnings"
    assert outcome.data["publication"]["state"] == "PUBLISHED"
    assert outcome.data["snapshot"]["published"] is True
    assert outcome.data["snapshot"]["truth_claim"] == "CAPTURED_STABLE"
    assert outcome.data["publication"]["bookkeeping"] == {
        "registry": "unknown",
        "run_file": "unknown",
    }
    assert outcome.warnings[0]["code"] == "POST_PUBLICATION_ANCILLARY_FAILURE"
    warning_message = outcome.warnings[0]["message"]
    assert "published successfully" in warning_message.lower()
    assert "current" not in warning_message.lower()
    status = service.status_service.status("repo-one")
    assert not (status.project and (status.project.last_status or "").startswith("INDEX_FAILED:"))
    query = runner.invoke(app, ["symbols", "repo-one", "--file", "module.py", "--json"])
    assert query.exit_code == 0


def test_post_publication_run_record_exception_is_only_a_warning(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n")
    RegistryService().register("repo-one", temp_git_repo)

    def fail_run_record(*args: object, **kwargs: object):
        raise OSError("induced ancillary failure")

    monkeypatch.setattr(IndexService, "_record_run_file", fail_run_record)
    outcome = IndexService().index("repo-one")

    assert outcome.result == "success_with_warnings"
    assert outcome.data["publication"]["state"] == "PUBLISHED"
    assert outcome.data["publication"]["bookkeeping"] == {
        "registry": "recorded",
        "run_file": "not_recorded",
    }
    assert any(
        warning["code"] == "POST_PUBLICATION_RUN_RECORD_NOT_WRITTEN" for warning in outcome.warnings
    )
    query = runner.invoke(app, ["inspect", "repo-one", "--file", "module.py", "--json"])
    assert query.exit_code == 0


def test_later_exception_preserves_completed_bookkeeping_facts(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    from project_kb.indexing import service as service_module

    real_outcome = service_module.IndexOutcome
    calls = 0

    def fail_first_response_assembly(**kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("later response assembly failure")
        return real_outcome(**kwargs)

    monkeypatch.setattr(service_module, "IndexOutcome", fail_first_response_assembly)
    service = IndexService()
    outcome = service.index("repo-one")

    assert outcome.result == "success_with_warnings"
    assert outcome.data["publication"]["state"] == "POST_PUBLICATION_RECORDED"
    assert outcome.data["publication"]["bookkeeping"] == {
        "registry": "recorded",
        "run_file": "recorded",
    }
    assert (
        Path(project.storage_path) / "runs" / f"{outcome.data['index_run']['run_id']}.json"
    ).is_file()
    status = service.status_service.status("repo-one")
    assert not (status.project and (status.project.last_status or "").startswith("INDEX_FAILED:"))
    query = runner.invoke(app, ["inspect", "repo-one", "--file", "module.py", "--json"])
    assert query.exit_code == 0


def test_missing_file_queries_fail_but_indexed_file_without_facts_succeeds(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "notes.txt").write_text("plain text\n")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")

    for command in ("symbols", "imports"):
        missing = runner.invoke(app, [command, "repo-one", "--file", "absent.py", "--json"])
        assert missing.exit_code == 21
        assert json.loads(missing.output)["code"] == "FILE_NOT_INDEXED"
        empty = runner.invoke(app, [command, "repo-one", "--file", "notes.txt", "--json"])
        assert empty.exit_code == 0
        assert json.loads(empty.output)["data"][command] == []


def test_query_after_failed_refresh_warns_and_exposes_provenance(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("def indexed():\n    return 1\n")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.indexing import service as service_module

    def fail_build(*args: object, **kwargs: object):
        raise IndexingError("induced refresh failure")

    monkeypatch.setattr(service_module, "write_snapshot", fail_build)
    with pytest.raises(IndexingError):
        IndexService().index("repo-one")

    result = runner.invoke(app, ["symbols", "repo-one", "--file", "module.py", "--json"])
    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["result"] == "success_with_warnings"
    assert payload["data"]["project_state"] == "LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE"
    assert payload["data"]["snapshot_availability"] == "available"
    assert payload["data"]["last_index_outcome"].startswith("INDEX_FAILED:")
    assert payload["warnings"][0]["code"] == "USING_PREVIOUS_SNAPSHOT_AFTER_FAILED_REFRESH"
    snapshot = payload["data"]["snapshot"]
    assert {
        "snapshot_id",
        "schema_version",
        "scanner_version",
        "policy_version",
        "extractor_versions",
        "compatibility",
    }.issubset(snapshot)
    symbol = next(item for item in payload["data"]["symbols"] if item["short_name"] == "indexed")
    assert symbol["content_hash"]
    assert symbol["extractor_name"] == "python-ast"
    assert symbol["extractor_version"] == "1"


def test_symbol_name_filter_is_literal_case_sensitive_prefix(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    (temp_git_repo / "names.py").write_text(
        "class PaymentService:\n"
        "    pass\n\n"
        "class payment_service:\n"
        "    pass\n\n"
        "def A_value():\n"
        "    pass\n\n"
        "def AXvalue():\n"
        "    pass\n"
    )
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"

    upper = runner.invoke(app, ["symbols", "repo-one", "--name", "Payment", "--json"])
    lower = runner.invoke(app, ["symbols", "repo-one", "--name", "payment", "--json"])
    underscore = runner.invoke(app, ["symbols", "repo-one", "--name", "A_", "--json"])
    empty = runner.invoke(app, ["symbols", "repo-one", "--name", "", "--json"])

    # Percent is not legal in a Python identifier. Exercise its literal SQL
    # escaping on a disposable validated copy, never the published canonical DB.
    query_fixture = tmp_path / "literal-filter.sqlite"
    shutil.copy2(database, query_fixture)
    reader = SnapshotReader(query_fixture, project_id=project.project_id)
    with contextlib.closing(sqlite3.connect(query_fixture)) as conn:
        conn.execute(
            "UPDATE symbols SET short_name = 'A%value', qualified_name = 'A%value' "
            "WHERE short_name = 'A_value'"
        )
        conn.commit()
    percent = reader.symbols(name="A%")

    assert [item["short_name"] for item in json.loads(upper.output)["data"]["symbols"]] == [
        "PaymentService"
    ]
    assert [item["short_name"] for item in json.loads(lower.output)["data"]["symbols"]] == [
        "payment_service"
    ]
    assert [item["short_name"] for item in json.loads(underscore.output)["data"]["symbols"]] == [
        "A_value"
    ]
    assert [item["short_name"] for item in percent] == ["A%value"]
    assert len(json.loads(empty.output)["data"]["symbols"]) >= 4


def test_structural_and_observation_fingerprints_have_distinct_semantics(
    temp_git_repo: Path,
) -> None:
    source = temp_git_repo / "same.py"
    source.write_text("VALUE = 1\n")
    RegistryService().register("repo-one", temp_git_repo)
    first = IndexService().index("repo-one")
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000_000))
    second = IndexService().index("repo-one")
    _git(temp_git_repo, "add", "same.py")
    third = IndexService().index("repo-one")

    assert (
        first.data["snapshot"]["logical_fingerprint"]
        == second.data["snapshot"]["logical_fingerprint"]
    )
    assert (
        second.data["snapshot"]["logical_fingerprint"]
        == third.data["snapshot"]["logical_fingerprint"]
    )
    assert (
        first.data["snapshot"]["observation_fingerprint"]
        != second.data["snapshot"]["observation_fingerprint"]
    )
    assert (
        second.data["snapshot"]["observation_fingerprint"]
        != third.data["snapshot"]["observation_fingerprint"]
    )


def test_human_inspect_and_requested_effective_modes(temp_git_repo: Path) -> None:
    (temp_git_repo / "module.py").write_text("def value():\n    return 1\n")
    RegistryService().register("repo-one", temp_git_repo)

    default = runner.invoke(app, ["index", "repo-one", "--json"])
    full = runner.invoke(app, ["index", "repo-one", "--full", "--json"])
    human = runner.invoke(app, ["inspect", "repo-one", "--file", "module.py"])

    default_run = json.loads(default.output)["data"]["index_run"]
    full_run = json.loads(full.output)["data"]["index_run"]
    assert (default_run["requested_mode"], default_run["effective_mode"]) == ("default", "full")
    assert (full_run["requested_mode"], full_run["effective_mode"]) == ("full", "full")
    for label in (
        "Path:",
        "Analysis:",
        "Language:",
        "Size:",
        "Lines:",
        "Hash:",
        "Parse:",
        "Symbols:",
        "Imports:",
        "Diagnostics:",
    ):
        assert label in human.output


def _indexed_project(temp_git_repo: Path):
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    return project, Path(project.storage_path) / "kb.sqlite"


def _git(repo: Path, *args: str) -> None:
    run_test_git(repo, *args)
