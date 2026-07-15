import contextlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from _git_support import git as run_test_git
from typer.testing import CliRunner

from project_kb.cli.app import app
from project_kb.errors import IndexingError
from project_kb.indexing.models import ScanFacts, ScanPolicy
from project_kb.indexing.policy import path_key
from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.snapshot.database import logical_snapshot_fingerprint

runner = CliRunner()


def test_full_index_and_structural_queries_cover_synthetic_matrix(
    temp_git_repo: Path,
    isolated_kb_home: Path,
) -> None:
    _write_matrix(temp_git_repo)
    registered = RegistryService().register("repo-one", temp_git_repo).project
    assert registered is not None

    outcome = IndexService().index("repo-one")

    assert outcome.result == "success_with_warnings"
    assert outcome.data["counts"]["candidates"] >= 9
    assert outcome.data["counts"]["parse_failures"] == 1
    database = Path(registered.storage_path) / "kb.sqlite"
    assert database.is_file()
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        files = {row["relative_path"]: dict(row) for row in conn.execute("SELECT * FROM files")}
        assert "ignored.py" not in files
        assert files["deleted.py"]["file_kind"] == "MISSING"
        assert files["untracked.py"]["git_population"] == "UNTRACKED"
        assert files[".env"]["file_kind"] == "HARD_SECRET"
        assert files[".env"]["content_hash"] is None
        assert files["bom.py"]["encoding"] == "UTF-8-BOM"
        assert files["legacy.py"]["file_kind"] == "UNSUPPORTED_ENCODING"
        assert files["binary.py"]["file_kind"] == "BINARY"
        assert files["large.py"]["file_kind"] == "LARGE"
        assert files["large.dat"]["analysis_level"] == "METADATA_ONLY"
        assert files["broken.py"]["parse_status"] == "FAILED"
        assert conn.execute("SELECT COUNT(*) FROM parse_diagnostics").fetchone()[0] == 1
        symbol_kinds = {row[0] for row in conn.execute("SELECT DISTINCT symbol_kind FROM symbols")}
        assert {"MODULE", "CLASS", "METHOD", "ASYNC_METHOD", "NESTED_FUNCTION"}.issubset(
            symbol_kinds
        )
        relation_kinds = {
            row[0] for row in conn.execute("SELECT DISTINCT relation_kind FROM relations")
        }
        assert {
            "FILE_DEFINES_SYMBOL",
            "SYMBOL_CONTAINS_SYMBOL",
            "FILE_IMPORTS_MODULE",
            "FILE_IMPORTS_FILE",
            "CLASS_INHERITS_EXPRESSION",
            "SYMBOL_HAS_DECORATOR",
        }.issubset(relation_kinds)
        statuses = {
            row[0] for row in conn.execute("SELECT DISTINCT resolution_status FROM imports")
        }
        assert {"EXACT", "EXTERNAL", "AMBIGUOUS"}.issubset(statuses)
        relative_submodule = conn.execute(
            """SELECT i.resolution_status, target.relative_path
               FROM imports i
               LEFT JOIN files target ON target.file_id = i.resolved_file_id
               WHERE i.module_text = '' AND i.imported_name = 'base'"""
        ).fetchone()
        assert tuple(relative_submodule) == ("EXACT", "pkg/base.py")
        assert (
            conn.execute("SELECT relative_path FROM pruned_roots").fetchone()[0] == "node_modules"
        )

    symbols = runner.invoke(app, ["symbols", "repo-one", "--file", "pkg/mod.py", "--json"])
    imports = runner.invoke(app, ["imports", "repo-one", "--file", "pkg/mod.py", "--json"])
    inspect = runner.invoke(app, ["inspect", "repo-one", "--file", "broken.py", "--json"])
    for result in (symbols, imports, inspect):
        assert result.exit_code == 0, result.output
    symbol_payload = json.loads(symbols.output)
    assert any(item["short_name"] == "method" for item in symbol_payload["data"]["symbols"])
    prefix = runner.invoke(app, ["symbols", "repo-one", "--name", "met", "--json"])
    assert prefix.exit_code == 0
    assert any(
        item["short_name"] == "method" for item in json.loads(prefix.output)["data"]["symbols"]
    )
    inspect_payload = json.loads(inspect.output)
    assert inspect_payload["data"]["inspection"]["parse_diagnostics"][0]["line"] == 1

    status = runner.invoke(app, ["status", "repo-one", "--json"])
    assert status.exit_code == 0
    assert json.loads(status.output)["data"]["project_state"] == "SNAPSHOT_PRESENT_UNVERIFIED"


def test_repeated_full_rebuild_is_logically_equivalent(temp_git_repo: Path) -> None:
    (temp_git_repo / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(temp_git_repo, "add", "module.py")
    RegistryService().register("repo-one", temp_git_repo)

    first = IndexService().index("repo-one")
    second = IndexService().index("repo-one")

    assert first.data["snapshot"]["snapshot_id"] != second.data["snapshot"]["snapshot_id"]
    assert (
        first.data["snapshot"]["logical_fingerprint"]
        == second.data["snapshot"]["logical_fingerprint"]
    )
    assert second.data["previous_snapshot"] == {"preserved": False, "replaced": True}


def test_policy_version_changes_logical_fingerprint(temp_git_repo: Path) -> None:
    del temp_git_repo
    facts = ScanFacts()
    first = logical_snapshot_fingerprint(facts, ScanPolicy(policy_version="policy-a"))
    second = logical_snapshot_fingerprint(facts, ScanPolicy(policy_version="policy-b"))

    assert first != second


def test_syntax_error_recovery_removes_stale_and_restores_current_facts(
    temp_git_repo: Path,
) -> None:
    source = temp_git_repo / "module.py"
    source.write_text("def old():\n    return 1\n", encoding="utf-8")
    _git(temp_git_repo, "add", "module.py")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")

    source.write_text("def broken(:\n", encoding="utf-8")
    IndexService().index("repo-one")
    with contextlib.closing(sqlite3.connect(Path(project.storage_path) / "kb.sqlite")) as conn:
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM symbols s
                   JOIN files f ON f.file_id=s.file_id
                   WHERE f.relative_path='module.py'"""
            ).fetchone()[0]
            == 0
        )

    source.write_text("def fixed():\n    return 2\n", encoding="utf-8")
    IndexService().index("repo-one")
    with contextlib.closing(sqlite3.connect(Path(project.storage_path) / "kb.sqlite")) as conn:
        names = {row[0] for row in conn.execute("SELECT short_name FROM symbols")}
        assert "fixed" in names
        assert "old" not in names


def test_repository_change_during_both_attempts_blocks_publication(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    from project_kb.indexing import service as service_module

    real_scan = service_module.scan_repository
    calls = 0

    def changing_scan(*args: object, **kwargs: object):
        nonlocal calls
        facts = real_scan(*args, **kwargs)
        calls += 1
        (temp_git_repo / f"change-{calls}.py").write_text("value = 1\n", encoding="utf-8")
        return facts

    monkeypatch.setattr(service_module, "scan_repository", changing_scan)
    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert calls == 2


def test_publication_failure_preserves_previous_snapshot(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    first = IndexService().index("repo-one")
    current = Path(project.storage_path) / "kb.sqlite"
    previous_bytes = current.read_bytes()
    from project_kb.indexing import service as service_module

    def fail_publish(*args: object, **kwargs: object) -> bool:
        raise IndexingError("induced publication failure", code="SNAPSHOT_PUBLICATION_FAILED")

    monkeypatch.setattr(service_module, "publish_snapshot", fail_publish)
    with pytest.raises(IndexingError):
        IndexService().index("repo-one")

    assert current.read_bytes() == previous_bytes
    status = runner.invoke(app, ["status", "repo-one", "--json"])
    assert status.exit_code == 0
    assert (
        json.loads(status.output)["data"]["project_state"]
        == "LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE"
    )
    with contextlib.closing(sqlite3.connect(current)) as conn:
        fingerprint = conn.execute("SELECT logical_fingerprint FROM snapshot_meta").fetchone()[0]
    assert fingerprint == first.data["snapshot"]["logical_fingerprint"]


def test_snapshot_build_failure_preserves_previous_snapshot(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    current = Path(project.storage_path) / "kb.sqlite"
    previous_bytes = current.read_bytes()
    from project_kb.indexing import service as service_module

    def fail_build(*args: object, **kwargs: object):
        raise IndexingError("induced build failure")

    monkeypatch.setattr(service_module, "write_snapshot", fail_build)
    with pytest.raises(IndexingError):
        IndexService().index("repo-one")
    assert current.read_bytes() == previous_bytes


def test_corrupt_snapshot_is_reported_without_replacement(temp_git_repo: Path) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    database.write_bytes(b"not sqlite")

    status = runner.invoke(app, ["status", "repo-one", "--json"])

    assert status.exit_code == 21
    assert json.loads(status.output)["data"]["project_state"] == "SNAPSHOT_STORAGE_ERROR"
    assert database.read_bytes() == b"not sqlite"

    rebuilt = runner.invoke(app, ["index", "repo-one", "--full", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    assert json.loads(rebuilt.output)["data"]["snapshot"]["published"] is True


def test_hard_secret_is_classified_without_opening_content(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = temp_git_repo / ".env"
    secret.write_text("TOKEN=private\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    original_open = Path.open

    def guarded_open(self: Path, *args: object, **kwargs: object):
        if self.name == ".env":
            raise AssertionError("hard-secret content was opened")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    outcome = IndexService().index("repo-one")

    assert outcome.data["counts"]["candidates"] == 2


def test_symlink_candidate_is_metadata_only_when_supported(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    link = temp_git_repo / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks are unavailable in this Windows environment.")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    with contextlib.closing(sqlite3.connect(Path(project.storage_path) / "kb.sqlite")) as conn:
        row = conn.execute(
            "SELECT file_kind, content_hash FROM files WHERE relative_path='linked.py'"
        ).fetchone()
    assert row == ("REDIRECT", None)


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific canonical path-key behavior")
def test_windows_path_keys_are_case_insensitive() -> None:
    assert path_key("Pkg/Module.py") == path_key("pkg/module.py")


def test_index_and_query_resolve_current_directory(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "current.py").write_text("def here():\n    return True\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    indexed = runner.invoke(app, ["index", "--full", "--json"])
    queried = runner.invoke(app, ["symbols", "--file", "current.py", "--json"])

    assert indexed.exit_code == 0, indexed.output
    assert queried.exit_code == 0, queried.output
    assert any(
        item["short_name"] == "here" for item in json.loads(queried.output)["data"]["symbols"]
    )


def test_index_cli_uses_failed_result_for_fatal_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_index(*args: object, **kwargs: object):
        raise IndexingError("induced fatal build")

    monkeypatch.setattr(IndexService, "index", fail_index)
    result = runner.invoke(app, ["index", "repo-one", "--json"])

    assert result.exit_code == 21
    assert json.loads(result.output)["result"] == "failed"


def test_query_rejects_parent_traversal(temp_git_repo: Path) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")

    result = runner.invoke(app, ["inspect", "repo-one", "--file", "../README.md", "--json"])

    assert result.exit_code == 21
    assert json.loads(result.output)["code"] == "INVALID_RELATIVE_PATH"


def _write_matrix(repo: Path) -> None:
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (repo / "ignored.py").write_text("IGNORED = True\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=private\n", encoding="utf-8")
    (repo / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")
    (repo / "bom.py").write_bytes(b"\xef\xbb\xbfVALUE = 1\n")
    (repo / "legacy.py").write_bytes("значение = 1\n".encode("cp1251"))
    (repo / "binary.py").write_bytes(b"VALUE\0binary")
    (repo / "large.py").write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    (repo / "large.dat").write_bytes(b"\0" * 1024)
    (repo / "deleted.py").write_text("DELETED = True\n", encoding="utf-8")
    (repo / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "pkg.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "pkg.py").write_text("VALUE = 3\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "tracked.py").write_text("TRACKED = True\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "base.py").write_text("class Base:\n    pass\n", encoding="utf-8")
    (repo / "pkg" / "mod.py").write_text(
        """from .base import Base
from . import base
import os
import pkg

def deco(value):
    return value

class Child(Base):
    @deco
    def method(self):
        def nested():
            return 1
        return nested()

    async def async_method(self):
        return 2
""",
        encoding="utf-8",
    )
    _git(
        repo,
        "add",
        ".gitignore",
        ".env",
        "bom.py",
        "legacy.py",
        "binary.py",
        "large.py",
        "large.dat",
        "deleted.py",
        "broken.py",
        "other",
        "src",
        "node_modules",
        "pkg",
    )
    (repo / "deleted.py").unlink()


def _git(repo: Path, *args: str) -> None:
    run_test_git(repo, *args)
