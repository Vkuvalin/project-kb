import sqlite3
from pathlib import Path

import pytest

from project_kb.errors import RegistryOperationError
from project_kb.registry import RegistryService
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.state import ProjectState

LEGACY_PROJECT_ID = "a" * 32


def test_missing_registry_lookup_does_not_create_home(isolated_kb_home: Path) -> None:
    service = RegistryService(home=isolated_kb_home)

    assert service.find_project_by_name("missing") is None
    assert not isolated_kb_home.exists()


def test_populated_v1_registry_migrates_once_without_data_loss(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    service = RegistryService(home=isolated_kb_home)

    first = service.find_project_by_name("legacy")
    second = service.find_project_by_name("legacy")

    assert first is not None
    assert second is not None
    assert first.project_id == LEGACY_PROJECT_ID
    assert first.repo_root == str(temp_git_repo.resolve())
    assert first.repo_fingerprint_json is None
    with sqlite3.connect(registry_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        original_event = conn.execute(
            "SELECT message FROM registry_events WHERE event_id = 'legacy-event-id'"
        ).fetchone()
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM registry_events WHERE event_type = 'schema_migrated'"
        ).fetchone()[0]

    assert "repo_fingerprint_json" in columns
    assert meta["schema_version"] == "2"
    assert meta["created_at"] == "2026-01-01T00:00:00Z"
    assert meta["tool_version"] == "0.1.0"
    assert project_count == 1
    assert original_event == ("Legacy registration event.",)
    assert migration_count == 1


def test_invalid_existing_registry_returns_registry_error_without_replacement(
    isolated_kb_home: Path,
) -> None:
    isolated_kb_home.mkdir(parents=True)
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        conn.execute("INSERT INTO unrelated (value) VALUES ('preserve-me')")

    with pytest.raises(RegistryOperationError):
        RegistryService(home=isolated_kb_home).find_project_by_name("missing")

    with sqlite3.connect(registry_path) as conn:
        value = conn.execute("SELECT value FROM unrelated").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}

    assert value == "preserve-me"
    assert "projects" not in tables


@pytest.mark.parametrize("missing_key", ["created_at", "tool_version"])
def test_v2_registry_missing_mandatory_meta_is_rejected_without_recreation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    missing_key: str,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    service.register("repo-one", temp_git_repo)
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (missing_key,))

    outcome = ProjectStatusService(home=isolated_kb_home).status("repo-one")

    assert outcome.code == "REGISTRY_ERROR"
    assert outcome.project_state is ProjectState.REGISTRY_ERROR
    assert outcome.error is not None
    with sqlite3.connect(registry_path) as conn:
        keys = {row[0] for row in conn.execute("SELECT key FROM meta")}
    assert missing_key not in keys


def test_valid_v2_registry_meta_is_accepted(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    registered = service.register("repo-one", temp_git_repo).project
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        meta_before = dict(conn.execute("SELECT key, value FROM meta"))

    found = service.find_project_by_name("repo-one")

    assert registered is not None
    assert found is not None
    assert found.project_id == registered.project_id
    with sqlite3.connect(registry_path) as conn:
        meta_after = dict(conn.execute("SELECT key, value FROM meta"))
    assert {"schema_version", "created_at", "tool_version"}.issubset(meta_after)
    assert meta_after == meta_before


def test_redirected_registry_file_is_rejected_without_migrating_target(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    external_home = tmp_path / "external-home"
    external_registry = _create_v1_registry(external_home, temp_git_repo)
    isolated_kb_home.mkdir(parents=True)
    redirected_registry = isolated_kb_home / "registry.sqlite"
    try:
        redirected_registry.symlink_to(external_registry)
    except OSError:
        pytest.skip("File symlinks are unavailable in this Windows environment.")

    with pytest.raises(RegistryOperationError):
        RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    with sqlite3.connect(external_registry) as conn:
        schema_version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert schema_version == "1"


def _create_v1_registry(home: Path, repo_root: Path) -> Path:
    home.mkdir(parents=True)
    registry_path = home / "registry.sqlite"
    storage_path = home / "projects" / LEGACY_PROJECT_ID
    with sqlite3.connect(registry_path) as conn:
        conn.executescript(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                project_name TEXT NOT NULL,
                project_name_norm TEXT NOT NULL UNIQUE,
                repo_root TEXT NOT NULL,
                repo_root_norm TEXT NOT NULL UNIQUE,
                storage_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_status TEXT,
                last_indexed_at TEXT NULL,
                last_git_commit TEXT NULL
            );
            CREATE TABLE registry_events (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NULL,
                project_name TEXT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO projects (
                project_id, project_name, project_name_norm, repo_root, repo_root_norm,
                storage_path, created_at, updated_at, last_status,
                last_indexed_at, last_git_commit
            )
            VALUES (?, 'legacy', 'legacy', ?, ?, ?, ?, ?, 'REGISTERED', NULL, NULL)
            """,
            (
                LEGACY_PROJECT_ID,
                str(repo_root.resolve()),
                str(repo_root.resolve()),
                str(storage_path.resolve()),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO registry_events (
                event_id, project_id, project_name, event_type, message, details_json, created_at
            )
            VALUES (
                'legacy-event-id', ?, 'legacy', 'register',
                'Legacy registration event.', NULL, '2026-01-01T00:00:00Z'
            )
            """,
            (LEGACY_PROJECT_ID,),
        )
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("created_at", "2026-01-01T00:00:00Z"),
                ("tool_version", "0.1.0"),
            ],
        )
    return registry_path
