import json
import sqlite3
from pathlib import Path

import pytest
from _stage6_support import write_gold_v1_snapshot

from project_kb.errors import RegistryOperationError
from project_kb.registry import RegistryService
from project_kb.registry.service import normalize_repo_root
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.state import ProjectState

LEGACY_PROJECT_ID = "a" * 32
REGISTERED_AT = "2026-01-01T00:00:00Z"
INDEXED_AT = "2026-01-01T00:01:00Z"
RELINKED_AT = "2026-01-01T00:02:00Z"
AFTER_RELINK_AT = "2026-01-01T00:03:00Z"


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
        workspaces = conn.execute(
            """SELECT workspace_root, workspace_root_norm, workspace_kind,
                      workspace_binding_generation
               FROM workspaces WHERE project_id = ?""",
            (LEGACY_PROJECT_ID,),
        ).fetchall()

    assert "repo_fingerprint_json" in columns
    assert {"repo_binding_generation", "snapshot_binding_generation"}.issubset(columns)
    assert meta["schema_version"] == "4"
    assert meta["created_at"] == "2026-01-01T00:00:00Z"
    assert meta["tool_version"] == "0.1.0"
    assert project_count == 1
    assert original_event == ("Legacy registration event.",)
    assert migration_count == 2
    assert workspaces == [
        (
            str(temp_git_repo.resolve()),
            normalize_repo_root(temp_git_repo.resolve()),
            "PRIMARY",
            first.repo_binding_generation,
        )
    ]


def test_v2_relink_marker_migrates_to_distinct_binding_generations(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    with sqlite3.connect(registry_path) as conn:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_fingerprint_json TEXT NULL")
        conn.execute("UPDATE projects SET last_status = 'RELINKED_REINDEX_REQUIRED'")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation != project.snapshot_binding_generation
    with sqlite3.connect(registry_path) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        workspace_binding = conn.execute(
            "SELECT workspace_binding_generation FROM workspaces WHERE project_id = ?",
            (project.project_id,),
        ).fetchone()[0]
    assert version == "4"
    assert workspace_binding == project.repo_binding_generation


def test_legacy_registry_without_relink_migrates_with_aligned_binding(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=INDEXED_AT,
        updated_at=INDEXED_AT,
        events=[("index-success", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT)],
    )

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation == project.snapshot_binding_generation


def test_legacy_relink_to_another_root_without_later_success_requires_rebuild(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    replacement = tmp_path / "replacement-repo"
    replacement.mkdir()
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=INDEXED_AT,
        updated_at=RELINKED_AT,
        repo_root=replacement,
        events=[
            ("index-success", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT),
            ("relink", "relink", {"repo_root": str(replacement)}, RELINKED_AT),
        ],
    )

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation != project.snapshot_binding_generation


def test_legacy_same_root_relink_without_later_success_requires_rebuild(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=INDEXED_AT,
        updated_at=RELINKED_AT,
        events=[
            ("index-success", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT),
            ("relink", "relink", {"repo_root": str(temp_git_repo)}, RELINKED_AT),
        ],
    )

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation != project.snapshot_binding_generation


def test_legacy_relink_followed_by_failed_index_still_requires_rebuild(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_FAILED:INDEXING_ERROR",
        last_indexed_at=INDEXED_AT,
        updated_at=AFTER_RELINK_AT,
        events=[
            ("index-success", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT),
            ("relink", "relink", {"repo_root": str(temp_git_repo)}, RELINKED_AT),
            (
                "index-failed",
                "index_outcome",
                {"status": "INDEX_FAILED:INDEXING_ERROR", "failure_code": "INDEXING_ERROR"},
                AFTER_RELINK_AT,
            ),
        ],
    )

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation != project.snapshot_binding_generation


def test_legacy_relink_followed_by_confirmed_success_restores_aligned_binding(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=AFTER_RELINK_AT,
        updated_at=AFTER_RELINK_AT,
        events=[
            ("index-success-before", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT),
            ("relink", "relink", {"repo_root": str(temp_git_repo)}, RELINKED_AT),
            (
                "index-success-after",
                "index_outcome",
                {"status": "INDEX_SUCCEEDED"},
                AFTER_RELINK_AT,
            ),
        ],
    )

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation == project.snapshot_binding_generation


@pytest.mark.parametrize("history", ["missing", "incomplete"])
def test_legacy_missing_or_incomplete_event_history_fails_closed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    history: str,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    events = []
    if history == "incomplete":
        events.append(("index-unknown", "index_outcome", None, INDEXED_AT))
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=INDEXED_AT,
        updated_at=INDEXED_AT,
        events=events,
    )

    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")

    assert project is not None
    assert project.repo_binding_generation != project.snapshot_binding_generation


def test_legacy_history_migration_is_idempotent(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=INDEXED_AT,
        updated_at=RELINKED_AT,
        events=[
            ("index-success", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT),
            ("relink", "relink", {"repo_root": str(temp_git_repo)}, RELINKED_AT),
        ],
    )
    service = RegistryService(home=isolated_kb_home)

    first = service.find_project_by_name("legacy")
    second = service.find_project_by_name("legacy")

    assert first is not None
    assert second is not None
    assert first.repo_binding_generation == second.repo_binding_generation
    assert first.snapshot_binding_generation == second.snapshot_binding_generation
    with sqlite3.connect(registry_path) as conn:
        migrations = conn.execute(
            "SELECT COUNT(*) FROM registry_events WHERE event_type = 'schema_migrated'"
        ).fetchone()[0]
    assert migrations == 2


def test_safely_migrated_valid_v1_snapshot_remains_bounded_legacy_readable(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path = _create_v1_registry(isolated_kb_home, temp_git_repo)
    _configure_legacy_history(
        registry_path,
        last_status="INDEX_SUCCEEDED",
        last_indexed_at=REGISTERED_AT,
        updated_at=INDEXED_AT,
        events=[
            ("index-success", "index_outcome", {"status": "INDEX_SUCCEEDED"}, INDEXED_AT),
        ],
    )
    project = RegistryService(home=isolated_kb_home).find_project_by_name("legacy")
    assert project is not None
    storage = Path(project.storage_path)
    (storage / "exports").mkdir(parents=True)
    (storage / "runs").mkdir()
    write_gold_v1_snapshot(
        storage / "kb.sqlite",
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )

    status = ProjectStatusService(home=isolated_kb_home).status("legacy")

    assert status.snapshot_check.availability == "AVAILABLE", status
    assert status.snapshot_check.compatibility == "LEGACY_V1"
    assert status.snapshot_check.currentness == "UNVERIFIED"
    assert status.availability.can_use_snapshot is True


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
                normalize_repo_root(repo_root.resolve()),
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


def _configure_legacy_history(
    registry_path: Path,
    *,
    last_status: str,
    last_indexed_at: str | None,
    updated_at: str,
    events: list[tuple[str, str, dict[str, object] | None, str]],
    repo_root: Path | None = None,
) -> None:
    with sqlite3.connect(registry_path) as conn:
        if repo_root is None:
            conn.execute(
                """UPDATE projects
                   SET last_status = ?, last_indexed_at = ?, updated_at = ?
                   WHERE project_id = ?""",
                (last_status, last_indexed_at, updated_at, LEGACY_PROJECT_ID),
            )
        else:
            resolved = str(repo_root.resolve())
            conn.execute(
                """UPDATE projects
                   SET repo_root = ?, repo_root_norm = ?, last_status = ?,
                       last_indexed_at = ?, updated_at = ?
                   WHERE project_id = ?""",
                (
                    resolved,
                    normalize_repo_root(repo_root.resolve()),
                    last_status,
                    last_indexed_at,
                    updated_at,
                    LEGACY_PROJECT_ID,
                ),
            )
        conn.executemany(
            """INSERT INTO registry_events (
                   event_id, project_id, project_name, event_type,
                   message, details_json, created_at
               ) VALUES (?, ?, 'legacy', ?, 'Legacy operation.', ?, ?)""",
            [
                (
                    event_id,
                    LEGACY_PROJECT_ID,
                    event_type,
                    json.dumps(details, sort_keys=True) if details is not None else None,
                    created_at,
                )
                for event_id, event_type, details, created_at in events
            ],
        )
