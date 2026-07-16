import os
import sqlite3
import time
from pathlib import Path

import pytest
from _git_support import git

import project_kb.registry.schema as registry_schema
import project_kb.registry.service as registry_service
from project_kb.errors import RegistryOperationError
from project_kb.registry.db import (
    immediate_registry_transaction,
    open_existing_registry,
)
from project_kb.registry.models import ProjectRecord
from project_kb.registry.schema import (
    META_TABLE_SQL,
    PROJECTS_TABLE_SQL,
    REGISTRY_EVENTS_TABLE_SQL,
)
from project_kb.registry.service import (
    RegistryService,
    _compare_and_swap_pointer,
    _compare_and_swap_workspace_state,
    new_id,
    normalize_repo_root,
    utc_now,
)
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.repo_identity import build_repository_fingerprint

TIMESTAMP = "2026-07-16T12:00:00Z"
TASK_STATES = (
    "DRAFT",
    "ACTIVE",
    "BLOCKED",
    "ACCEPTED",
    "ABANDONED",
    "CLOSED",
)
TASK_STATE_PATHS = {
    "DRAFT": (),
    "ACTIVE": ("ACTIVE",),
    "BLOCKED": ("BLOCKED",),
    "ACCEPTED": ("ACTIVE", "ACCEPTED"),
    "ABANDONED": ("ABANDONED",),
    "CLOSED": ("ABANDONED", "CLOSED"),
}
OPERATION_PHASES = (
    "RESERVED",
    "BUILDING",
    "FILE_PUBLISHED",
    "COMMITTED",
    "FAILED",
    "RECOVERY_REQUIRED",
)
ALLOWED_OPERATION_TRANSITIONS = {
    ("RESERVED", "RESERVED"),
    ("RESERVED", "BUILDING"),
    ("RESERVED", "FAILED"),
    ("RESERVED", "RECOVERY_REQUIRED"),
    ("BUILDING", "BUILDING"),
    ("BUILDING", "FILE_PUBLISHED"),
    ("BUILDING", "FAILED"),
    ("BUILDING", "RECOVERY_REQUIRED"),
    ("FILE_PUBLISHED", "FILE_PUBLISHED"),
    ("FILE_PUBLISHED", "COMMITTED"),
    ("FILE_PUBLISHED", "RECOVERY_REQUIRED"),
    ("RECOVERY_REQUIRED", "RECOVERY_REQUIRED"),
    ("RECOVERY_REQUIRED", "COMMITTED"),
    ("RECOVERY_REQUIRED", "FAILED"),
}
OPERATION_PHASE_PATHS = {
    "RESERVED": (),
    "BUILDING": ("BUILDING",),
    "FILE_PUBLISHED": ("BUILDING", "FILE_PUBLISHED"),
    "COMMITTED": ("BUILDING", "FILE_PUBLISHED", "COMMITTED"),
    "FAILED": ("FAILED",),
    "RECOVERY_REQUIRED": ("RECOVERY_REQUIRED",),
}
GENERATION_STATES = (
    "AVAILABLE",
    "ORPHANED",
    "QUARANTINED",
    "PENDING_DELETE",
    "DELETED",
)
ALLOWED_GENERATION_TRANSITIONS = {
    ("AVAILABLE", "ORPHANED"),
    ("AVAILABLE", "QUARANTINED"),
    ("AVAILABLE", "PENDING_DELETE"),
    ("ORPHANED", "QUARANTINED"),
    ("ORPHANED", "PENDING_DELETE"),
    ("QUARANTINED", "PENDING_DELETE"),
    ("PENDING_DELETE", "DELETED"),
}
INITIAL_GENERATION_STATES = ("AVAILABLE", "ORPHANED", "QUARANTINED")
GENERATION_STATE_PATHS = {
    "AVAILABLE": (),
    "ORPHANED": (),
    "QUARANTINED": (),
    "PENDING_DELETE": ("PENDING_DELETE",),
    "DELETED": ("PENDING_DELETE", "DELETED"),
}


def test_v3_migration_backfills_exact_primary_and_does_not_merge_common_git_directory(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    linked = tmp_path / "linked-worktree"
    git(temp_git_repo, "worktree", "add", "--detach", str(linked), text=True)
    registry_path, expected = _create_v3_registry(
        isolated_kb_home,
        [("main", temp_git_repo), ("linked", linked)],
    )

    projects = RegistryService(home=isolated_kb_home).list_projects()

    assert len(projects) == 2
    assert {project.project_name for project in projects} == {"main", "linked"}
    assert len({project.project_id for project in projects}) == 2
    assert len({project.workspace_id for project in projects}) == 2
    with sqlite3.connect(registry_path) as conn:
        conn.row_factory = sqlite3.Row
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        workspaces = conn.execute("SELECT * FROM workspaces ORDER BY project_id").fetchall()
    assert version == "4"
    assert len(workspaces) == 2
    for workspace in workspaces:
        baseline = expected[workspace["project_id"]]
        assert workspace["workspace_kind"] == "PRIMARY"
        assert workspace["workspace_root"] == baseline["repo_root"]
        assert workspace["workspace_root_norm"] == baseline["repo_root_norm"]
        assert workspace["repository_fingerprint_json"] == baseline["fingerprint"]
        assert workspace["workspace_binding_generation"] == baseline["binding"]


def test_unsupported_future_version_performs_zero_mutation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    registry_path, _ = _create_v3_registry(
        isolated_kb_home,
        [("repo", temp_git_repo)],
    )
    with sqlite3.connect(registry_path) as conn:
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    before = registry_path.read_bytes()

    with pytest.raises(RegistryOperationError):
        RegistryService(home=isolated_kb_home).find_project_by_name("repo")

    assert registry_path.read_bytes() == before
    with sqlite3.connect(registry_path) as conn:
        assert (
            conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            == "999"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'workspaces'").fetchone()[
                0
            ]
            == 0
        )


def test_injected_mid_migration_failure_rolls_back_every_v4_change(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path, _ = _create_v3_registry(
        isolated_kb_home,
        [("repo", temp_git_repo)],
    )

    def fail_after_backfill(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setattr(registry_schema, "_create_v4_triggers", fail_after_backfill)
    with pytest.raises(RegistryOperationError):
        RegistryService(home=isolated_kb_home).find_project_by_name("repo")

    with sqlite3.connect(registry_path) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        migrations = conn.execute(
            "SELECT COUNT(*) FROM registry_events WHERE event_type = 'schema_migrated'"
        ).fetchone()[0]
    assert version == "3"
    assert "workspaces" not in tables
    assert "lifecycle_tasks" not in tables
    assert migrations == 0


@pytest.mark.parametrize(
    ("object_type", "object_name"),
    [
        ("INDEX", "ux_tasks_open_workspace_binding"),
        ("TRIGGER", "projects_workspace_projection_guard"),
    ],
)
def test_schema_manifest_rejects_missing_index_or_trigger(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    object_type: str,
    object_name: str,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    service.register("repo", temp_git_repo)
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(f"DROP {object_type} {object_name}")

    with pytest.raises(RegistryOperationError):
        service.find_project_by_name("repo")


def test_schema_manifest_rejects_foreign_key_or_constraint_definition_tampering(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    service.register("repo", temp_git_repo)
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            """UPDATE sqlite_master
               SET sql = replace(sql, 'ON UPDATE RESTRICT ON DELETE RESTRICT',
                                      'ON UPDATE NO ACTION ON DELETE NO ACTION')
               WHERE type = 'table' AND name = 'workspaces'"""
        )
        conn.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(RegistryOperationError):
        service.find_project_by_name("repo")


def test_direct_project_projection_drift_is_rejected_and_workspace_update_projects_atomically(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="projection drift"):
            conn.execute(
                "UPDATE projects SET repo_root = 'drifted' WHERE project_id = ?",
                (project.project_id,),
            )

        new_binding = new_id()
        fingerprint = build_repository_fingerprint(second_temp_git_repo).to_json()
        conn.execute(
            """UPDATE workspaces
               SET workspace_root = ?, workspace_root_norm = ?,
                   repository_fingerprint_json = ?, workspace_binding_generation = ?,
                   row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = ?""",
            (
                str(second_temp_git_repo.resolve()),
                normalize_repo_root(second_temp_git_repo.resolve()),
                fingerprint,
                new_binding,
                TIMESTAMP,
                project.workspace_id,
            ),
        )
        projection = conn.execute(
            """SELECT repo_root, repo_root_norm, repo_fingerprint_json,
                      repo_binding_generation
               FROM projects WHERE project_id = ?""",
            (project.project_id,),
        ).fetchone()
    assert projection == (
        str(second_temp_git_repo.resolve()),
        normalize_repo_root(second_temp_git_repo.resolve()),
        fingerprint,
        new_binding,
    )
    resolved = service.find_project_by_repo_root(second_temp_git_repo.resolve())
    assert resolved is not None
    assert resolved.workspace_id == project.workspace_id
    assert resolved.repo_binding_generation == new_binding


@pytest.mark.parametrize("workspace_state", ["UNAVAILABLE", "RELINK_REQUIRED", "RETIRED"])
def test_open_task_requires_active_workspace(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    workspace_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = ?, row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = ?""",
            (workspace_state, TIMESTAMP, project.workspace_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="active workspace binding"):
            _insert_task(conn, project, task_id="a" * 32, state="DRAFT")


@pytest.mark.parametrize("workspace_state", ["UNAVAILABLE", "RELINK_REQUIRED", "RETIRED"])
def test_active_operation_requires_active_workspace(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    workspace_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = ?, row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = ?""",
            (workspace_state, TIMESTAMP, project.workspace_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="active workspace binding"):
            _insert_operation(
                conn,
                project,
                operation_id="b" * 32,
                phase="RESERVED",
            )


def test_active_operation_same_phase_update_requires_active_workspace(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    operation_id = "0" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_operation(conn, project, operation_id=operation_id, phase="RESERVED")
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'UNAVAILABLE', row_version = row_version + 1,
                   updated_at = ? WHERE workspace_id = ?""",
            (TIMESTAMP, project.workspace_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="active workspace binding"):
            conn.execute(
                """UPDATE lifecycle_operations
                   SET lease_owner = 'retry', row_version = row_version + 1, updated_at = ?
                   WHERE operation_id = ?""",
                (TIMESTAMP, operation_id),
            )


@pytest.mark.parametrize("owner", ["task", "operation"])
@pytest.mark.parametrize(
    ("column", "value_factory"),
    [
        ("workspace_root", lambda project: f"{project.repo_root}-changed"),
        ("workspace_root_norm", lambda project: f"{project.repo_root_norm}-changed"),
        ("repository_fingerprint_json", lambda _project: '{"changed":true}'),
        ("workspace_binding_generation", lambda _project: "f" * 32),
    ],
)
def test_open_lifecycle_work_freezes_workspace_binding_evidence(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    owner: str,
    column: str,
    value_factory: object,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if owner == "task":
            _insert_task(conn, project, task_id="c" * 32, state="ACTIVE")
        else:
            _insert_operation(
                conn,
                project,
                operation_id="d" * 32,
                phase="RECOVERY_REQUIRED",
            )
        value = value_factory(project)  # type: ignore[operator]
        with pytest.raises(sqlite3.IntegrityError, match="open lifecycle work"):
            conn.execute(
                f"""UPDATE workspaces
                    SET {column} = ?, row_version = row_version + 1, updated_at = ?
                    WHERE workspace_id = ?""",
                (value, TIMESTAMP, project.workspace_id),
            )


@pytest.mark.parametrize("owner", ["task", "operation"])
def test_open_lifecycle_work_blocks_workspace_retirement(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    owner: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if owner == "task":
            _insert_task(conn, project, task_id="e" * 32, state="BLOCKED")
        else:
            _insert_operation(conn, project, operation_id="f" * 32, phase="BUILDING")
        with pytest.raises(sqlite3.IntegrityError, match="workspace retirement"):
            conn.execute(
                """UPDATE workspaces
                   SET workspace_state = 'RETIRED', row_version = row_version + 1,
                       updated_at = ? WHERE workspace_id = ?""",
                (TIMESTAMP, project.workspace_id),
            )


def test_retired_workspace_cannot_reopen(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'RETIRED', row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = ?""",
            (TIMESTAMP, project.workspace_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="workspace immutable"):
            conn.execute(
                """UPDATE workspaces
                   SET workspace_state = 'ACTIVE', row_version = row_version + 1, updated_at = ?
                   WHERE workspace_id = ?""",
                (TIMESTAMP, project.workspace_id),
            )


@pytest.mark.parametrize(
    ("column", "new_value"),
    [
        ("workspace_root", "C:/retired/rewritten"),
        ("workspace_root_norm", "c:/retired/rewritten"),
        ("repository_fingerprint_json", '{"rewritten":true}'),
        ("workspace_binding_generation", "f" * 32),
    ],
)
def test_retired_workspace_physical_identity_is_fully_immutable(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    column: str,
    new_value: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'RETIRED', row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = ?""",
            (TIMESTAMP, project.workspace_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="workspace immutable"):
            conn.execute(
                f"""UPDATE workspaces
                    SET {column} = ?, row_version = row_version + 1, updated_at = ?
                    WHERE workspace_id = ?""",
                (new_value, TIMESTAMP, project.workspace_id),
            )


def test_workspace_retirement_cannot_rewrite_physical_identity_atomically(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with (
        sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn,
        pytest.raises(sqlite3.IntegrityError, match="workspace immutable"),
    ):
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'RETIRED', workspace_root = ?,
                   row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = ?""",
            ("C:/retired/rewritten", TIMESTAMP, project.workspace_id),
        )


def test_legacy_fingerprint_initialization_remains_allowed_before_lifecycle_work(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute(
            """UPDATE workspaces
               SET repository_fingerprint_json = NULL, row_version = row_version + 1,
                   updated_at = ? WHERE workspace_id = ?""",
            (TIMESTAMP, project.workspace_id),
        )
    current = service.find_project_by_name("repo")
    assert current is not None

    initialized, changed = service.initialize_repo_fingerprint(
        current.project_id,
        build_repository_fingerprint(temp_git_repo),
    )

    assert changed is True
    assert initialized.repo_fingerprint_json is not None


@pytest.mark.parametrize("initial_state", TASK_STATES)
def test_lifecycle_task_registration_must_start_in_draft(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    initial_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if initial_state == "DRAFT":
            _insert_task_row(conn, project, task_id="0" * 32, state=initial_state)
            assert conn.execute("SELECT task_state FROM lifecycle_tasks").fetchone() == ("DRAFT",)
        else:
            with pytest.raises(sqlite3.IntegrityError, match="must start in DRAFT"):
                _insert_task_row(conn, project, task_id="0" * 32, state=initial_state)


@pytest.mark.parametrize(
    ("column", "initial_value", "replacement"),
    [
        ("project_baseline_snapshot_id", "1" * 32, "2" * 32),
        ("project_baseline_pointer_version", 1, 999),
    ],
)
def test_task_project_baseline_expectations_are_immutable(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    column: str,
    initial_value: object,
    replacement: object,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "3" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task_row(
            conn,
            project,
            task_id=task_id,
            state="DRAFT",
            project_baseline_snapshot_id="1" * 32,
            project_baseline_pointer_version=1,
        )
        with pytest.raises(sqlite3.IntegrityError, match="task ownership, transition"):
            conn.execute(
                f"""UPDATE lifecycle_tasks
                    SET {column} = ?, row_version = row_version + 1, updated_at = ?
                    WHERE task_id = ?""",
                (replacement, TIMESTAMP, task_id),
            )
        assert conn.execute(
            f"SELECT {column} FROM lifecycle_tasks WHERE task_id = ?", (task_id,)
        ).fetchone() == (initial_value,)


@pytest.mark.parametrize("task_state", ["DRAFT", "ACTIVE", "BLOCKED"])
def test_nonterminal_task_same_state_evidence_updates_remain_allowed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    task_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "4" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state=task_state)
        conn.execute(
            """UPDATE lifecycle_tasks
               SET next_generation_sequence = 1, row_version = row_version + 1, updated_at = ?
               WHERE task_id = ?""",
            (TIMESTAMP, task_id),
        )
        assert conn.execute(
            "SELECT task_state, next_generation_sequence FROM lifecycle_tasks"
        ).fetchone() == (task_state, 1)


@pytest.mark.parametrize("task_state", ["ACCEPTED", "ABANDONED", "CLOSED"])
def test_terminal_task_rows_cannot_be_rewritten_in_place(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    task_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "5" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state=task_state)
        with pytest.raises(sqlite3.IntegrityError, match="task ownership, transition"):
            conn.execute(
                """UPDATE lifecycle_tasks
                   SET next_generation_sequence = 999,
                       row_version = row_version + 1, updated_at = ?
                   WHERE task_id = ?""",
                (TIMESTAMP, task_id),
            )


@pytest.mark.parametrize("task_state", ["ACCEPTED", "ABANDONED"])
def test_terminal_task_transition_to_closed_remains_allowed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    task_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "6" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state=task_state)
        conn.execute(
            """UPDATE lifecycle_tasks
               SET task_state = 'CLOSED', row_version = row_version + 1, updated_at = ?
               WHERE task_id = ?""",
            (TIMESTAMP, task_id),
        )
        assert conn.execute(
            "SELECT task_state FROM lifecycle_tasks WHERE task_id = ?", (task_id,)
        ).fetchone() == ("CLOSED",)


@pytest.mark.parametrize("initial_phase", OPERATION_PHASES)
def test_lifecycle_operation_registration_must_start_in_reserved(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    initial_phase: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if initial_phase == "RESERVED":
            _insert_operation_row(
                conn,
                project,
                operation_id="7" * 32,
                phase=initial_phase,
            )
            assert conn.execute("SELECT operation_phase FROM lifecycle_operations").fetchone() == (
                "RESERVED",
            )
        else:
            with pytest.raises(sqlite3.IntegrityError, match="must start in RESERVED"):
                _insert_operation_row(
                    conn,
                    project,
                    operation_id="7" * 32,
                    phase=initial_phase,
                )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("expected_task_version", 999),
        ("expected_pointer_versions_json", '{"TASK_BASELINE":999}'),
    ],
)
def test_operation_cas_expectations_are_immutable(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    column: str,
    replacement: object,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    operation_id = "8" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_operation_row(
            conn,
            project,
            operation_id=operation_id,
            phase="RESERVED",
            expected_task_version=1,
            expected_pointer_versions_json='{"TASK_BASELINE":1}',
        )
        with pytest.raises(sqlite3.IntegrityError, match="operation ownership, transition"):
            conn.execute(
                f"""UPDATE lifecycle_operations
                    SET {column} = ?, row_version = row_version + 1, updated_at = ?
                    WHERE operation_id = ?""",
                (replacement, TIMESTAMP, operation_id),
            )


@pytest.mark.parametrize("old_phase", OPERATION_PHASES)
@pytest.mark.parametrize("new_phase", OPERATION_PHASES)
def test_operation_phase_transition_graph_is_machine_enforced(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    old_phase: str,
    new_phase: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    operation_id = "1" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_operation(conn, project, operation_id=operation_id, phase=old_phase)
        statement = """UPDATE lifecycle_operations
                       SET operation_phase = ?, lease_owner = 'worker',
                           row_version = row_version + 1, updated_at = ?
                       WHERE operation_id = ?"""
        parameters = (new_phase, TIMESTAMP, operation_id)
        if (old_phase, new_phase) in ALLOWED_OPERATION_TRANSITIONS:
            conn.execute(statement, parameters)
            row = conn.execute(
                "SELECT operation_phase, lease_owner FROM lifecycle_operations"
            ).fetchone()
            assert row == (new_phase, "worker")
        else:
            with pytest.raises(sqlite3.IntegrityError, match="operation ownership, transition"):
                conn.execute(statement, parameters)


@pytest.mark.parametrize("initial_state", GENERATION_STATES)
def test_snapshot_generation_registration_uses_only_approved_initial_states(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    initial_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "9" * 32
    operation_id = "a" * 32
    snapshot_id = "b" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="CLOSED")
        _insert_committed_operation(conn, project, task_id, operation_id)
        if initial_state in INITIAL_GENERATION_STATES:
            _insert_generation_row(
                conn,
                project,
                task_id,
                operation_id,
                snapshot_id,
                0,
                None,
                state=initial_state,
            )
            assert conn.execute("SELECT generation_state FROM snapshot_generations").fetchone() == (
                initial_state,
            )
        else:
            with pytest.raises(sqlite3.IntegrityError, match="invalid initial state"):
                _insert_generation_row(
                    conn,
                    project,
                    task_id,
                    operation_id,
                    snapshot_id,
                    0,
                    None,
                    state=initial_state,
                )


@pytest.mark.parametrize("old_state", GENERATION_STATES)
@pytest.mark.parametrize("new_state", GENERATION_STATES)
def test_generation_state_transition_graph_is_machine_enforced(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    old_state: str,
    new_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "2" * 32
    operation_id = "3" * 32
    snapshot_id = "4" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="CLOSED")
        _insert_committed_operation(conn, project, task_id, operation_id)
        _insert_generation(
            conn,
            project,
            task_id,
            operation_id,
            snapshot_id,
            0,
            None,
            state=old_state,
        )
        statement = """UPDATE snapshot_generations
                       SET generation_state = ?, row_version = row_version + 1,
                           updated_at = ? WHERE snapshot_id = ?"""
        parameters = (new_state, TIMESTAMP, snapshot_id)
        if (old_state, new_state) in ALLOWED_GENERATION_TRANSITIONS:
            conn.execute(statement, parameters)
            assert (
                conn.execute(
                    "SELECT generation_state FROM snapshot_generations WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()[0]
                == new_state
            )
        else:
            with pytest.raises(sqlite3.IntegrityError, match="generation identity, transition"):
                conn.execute(statement, parameters)


@pytest.mark.parametrize(
    "task_state",
    ["DRAFT", "ACTIVE", "BLOCKED", "ACCEPTED", "ABANDONED"],
)
def test_unpointed_generation_of_nonclosed_task_cannot_enter_cleanup(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    task_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "c" * 32
    operation_id = "d" * 32
    snapshot_id = "e" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state=task_state)
        _insert_committed_operation(conn, project, task_id, operation_id)
        _insert_generation(conn, project, task_id, operation_id, snapshot_id, 0, None)
        with pytest.raises(sqlite3.IntegrityError, match="requires a CLOSED owning task"):
            conn.execute(
                """UPDATE snapshot_generations
                   SET generation_state = 'PENDING_DELETE', row_version = row_version + 1,
                       updated_at = ? WHERE snapshot_id = ?""",
                (TIMESTAMP, snapshot_id),
            )


def test_closed_task_unpointed_generation_can_enter_cleanup(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "f" * 32
    operation_id = "0" * 32
    snapshot_id = "1" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="CLOSED")
        _insert_committed_operation(conn, project, task_id, operation_id)
        _insert_generation(conn, project, task_id, operation_id, snapshot_id, 0, None)
        conn.execute(
            """UPDATE snapshot_generations
               SET generation_state = 'PENDING_DELETE', row_version = row_version + 1,
                   updated_at = ? WHERE snapshot_id = ?""",
            (TIMESTAMP, snapshot_id),
        )
        assert conn.execute("SELECT generation_state FROM snapshot_generations").fetchone() == (
            "PENDING_DELETE",
        )


@pytest.mark.parametrize(
    "operation_phase",
    ["RESERVED", "BUILDING", "FILE_PUBLISHED", "RECOVERY_REQUIRED"],
)
def test_unresolved_operation_reserved_snapshot_protects_generation_from_cleanup(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    operation_phase: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "2" * 32
    origin_operation_id = "3" * 32
    protection_operation_id = "4" * 32
    snapshot_id = "5" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="CLOSED")
        _insert_committed_operation(conn, project, task_id, origin_operation_id)
        _insert_generation(
            conn,
            project,
            task_id,
            origin_operation_id,
            snapshot_id,
            0,
            None,
        )
        _insert_operation(
            conn,
            project,
            operation_id=protection_operation_id,
            phase=operation_phase,
            task_id=task_id,
            reserved_snapshot_id=snapshot_id,
        )
        with pytest.raises(sqlite3.IntegrityError, match="unresolved operation protects"):
            conn.execute(
                """UPDATE snapshot_generations
                   SET generation_state = 'PENDING_DELETE', row_version = row_version + 1,
                       updated_at = ? WHERE snapshot_id = ?""",
                (TIMESTAMP, snapshot_id),
            )


def test_nonclosed_task_generation_can_still_fail_closed_to_quarantined(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "6" * 32
    operation_id = "7" * 32
    snapshot_id = "8" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="ACTIVE")
        _insert_committed_operation(conn, project, task_id, operation_id)
        _insert_generation(conn, project, task_id, operation_id, snapshot_id, 0, None)
        conn.execute(
            """UPDATE snapshot_generations
               SET generation_state = 'QUARANTINED', row_version = row_version + 1,
                   updated_at = ? WHERE snapshot_id = ?""",
            (TIMESTAMP, snapshot_id),
        )
        assert conn.execute("SELECT generation_state FROM snapshot_generations").fetchone() == (
            "QUARANTINED",
        )


def test_project_baseline_always_protects_available_generation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "5" * 32
    baseline_id = "6" * 32
    final_id = "7" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="CLOSED")
        _insert_committed_operation(conn, project, task_id, "8" * 32)
        _insert_generation(conn, project, task_id, "8" * 32, baseline_id, 0, None)
        _insert_committed_operation(conn, project, task_id, "9" * 32)
        _insert_generation(
            conn,
            project,
            task_id,
            "9" * 32,
            final_id,
            1,
            baseline_id,
            purpose="TASK_FINAL",
        )
        _insert_pointer(
            conn,
            project,
            pointer_id="a" * 32,
            pointer_role="PROJECT_BASELINE",
            snapshot_id=final_id,
            task_id=None,
        )
        with pytest.raises(sqlite3.IntegrityError, match="remain available"):
            conn.execute(
                """UPDATE snapshot_generations
                   SET generation_state = 'PENDING_DELETE', row_version = row_version + 1,
                       updated_at = ? WHERE snapshot_id = ?""",
                (TIMESTAMP, final_id),
            )


@pytest.mark.parametrize(
    "task_state",
    ["DRAFT", "ACTIVE", "BLOCKED", "ACCEPTED", "ABANDONED", "CLOSED"],
)
def test_task_pointer_protection_ends_only_when_task_is_closed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    task_state: str,
) -> None:
    project = RegistryService(home=isolated_kb_home).register("repo", temp_git_repo).project
    assert project is not None
    task_id = "b" * 32
    snapshot_id = "c" * 32
    pointer_id = "d" * 32
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state=task_state)
        _insert_committed_operation(conn, project, task_id, "e" * 32)
        _insert_generation(conn, project, task_id, "e" * 32, snapshot_id, 0, None)
        _insert_pointer(
            conn,
            project,
            pointer_id=pointer_id,
            pointer_role="TASK_BASELINE",
            snapshot_id=snapshot_id,
            task_id=task_id,
        )
        statement = """UPDATE snapshot_generations
                       SET generation_state = 'PENDING_DELETE',
                           row_version = row_version + 1, updated_at = ?
                       WHERE snapshot_id = ?"""
        if task_state == "CLOSED":
            conn.execute(statement, (TIMESTAMP, snapshot_id))
            historical_pointer = conn.execute(
                """SELECT snapshot_id FROM managed_pointers
                   WHERE pointer_id = ?""",
                (pointer_id,),
            ).fetchone()
            assert historical_pointer == (snapshot_id,)
        else:
            with pytest.raises(sqlite3.IntegrityError, match="remain available"):
                conn.execute(statement, (TIMESTAMP, snapshot_id))


def test_workspace_row_cas_rejects_stale_version(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None

    assert not hasattr(service, "compare_and_swap_workspace_state")
    with open_existing_registry(home=isolated_kb_home, now=utc_now, event_id=new_id) as conn:
        assert conn is not None
        with immediate_registry_transaction(conn):
            updated_version = _compare_and_swap_workspace_state(
                conn,
                project.workspace_id,
                expected_row_version=project.workspace_row_version,
                expected_state="ACTIVE",
                new_state="UNAVAILABLE",
                updated_at=TIMESTAMP,
            )
        updated = service.find_workspace_by_id(project.workspace_id)
        assert updated is not None
        with (
            pytest.raises(RegistryOperationError, match="compare-and-swap"),
            immediate_registry_transaction(conn),
        ):
            _compare_and_swap_workspace_state(
                conn,
                project.workspace_id,
                expected_row_version=project.workspace_row_version,
                expected_state="ACTIVE",
                new_state="ACTIVE",
                updated_at=TIMESTAMP,
            )

    assert updated_version == project.workspace_row_version + 1
    assert updated.row_version == project.workspace_row_version + 1
    assert updated.workspace_state == "UNAVAILABLE"


def test_immediate_registry_boundary_rejects_disabled_foreign_keys() -> None:
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(RegistryOperationError, match="foreign-key enforcement"),
        immediate_registry_transaction(conn),
    ):
        pass


def test_two_connections_have_bounded_lock_and_database_enforced_open_lineage(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None

    with (
        open_existing_registry(home=isolated_kb_home, now=utc_now, event_id=new_id) as first,
        open_existing_registry(home=isolated_kb_home, now=utc_now, event_id=new_id) as second,
    ):
        assert first is not None
        assert second is not None
        with immediate_registry_transaction(first):
            _insert_task(first, project, task_id="1" * 32, state="ACTIVE")
            started = time.monotonic()
            with (
                pytest.raises(sqlite3.OperationalError, match="locked"),
                immediate_registry_transaction(second),
            ):
                pass
            elapsed = time.monotonic() - started
            assert elapsed < 2.5

        with (
            pytest.raises(sqlite3.IntegrityError),
            immediate_registry_transaction(second),
        ):
            _insert_task(second, project, task_id="2" * 32, state="DRAFT")


def test_foreign_key_ownership_and_destructive_history_attacks_fail_closed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    first = service.register("first", temp_git_repo).project
    second = service.register("second", second_temp_git_repo).project
    assert first is not None
    assert second is not None
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(
                conn,
                first,
                task_id="3" * 32,
                state="ACTIVE",
                workspace_id=second.workspace_id,
            )

        _insert_task(conn, first, task_id="4" * 32, state="ACTIVE")
        with pytest.raises(sqlite3.IntegrityError, match="history cannot be deleted"):
            conn.execute("DELETE FROM lifecycle_tasks WHERE task_id = ?", ("4" * 32,))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle-bearing"):
            conn.execute("DELETE FROM workspaces WHERE workspace_id = ?", (first.workspace_id,))
        with pytest.raises(sqlite3.IntegrityError, match="workspace authority"):
            conn.execute("DELETE FROM projects WHERE project_id = ?", (first.project_id,))


def test_pointer_cas_is_forward_only_and_stale_updates_fail(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    pointer_id = "f" * 32
    snapshots = ["a" * 32, "b" * 32, "c" * 32]
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        task_id = "5" * 32
        _insert_task(conn, project, task_id=task_id, state="ACTIVE")
        parent: str | None = None
        for sequence, snapshot_id in enumerate(snapshots):
            operation_id = f"{sequence + 6:x}" * 32
            _insert_committed_operation(conn, project, task_id, operation_id)
            _insert_generation(
                conn,
                project,
                task_id,
                operation_id,
                snapshot_id,
                sequence,
                parent,
            )
            parent = snapshot_id
        with pytest.raises(sqlite3.IntegrityError, match="available owned generation"):
            conn.execute(
                """INSERT INTO managed_pointers (
                       pointer_id, project_id, task_id, pointer_role, snapshot_id,
                       pointer_version, created_at, updated_at
                   ) VALUES (?, ?, ?, 'TASK_FINAL', ?, 0, ?, ?)""",
                ("0" * 32, project.project_id, task_id, snapshots[2], TIMESTAMP, TIMESTAMP),
            )
        conn.execute(
            """INSERT INTO managed_pointers (
                   pointer_id, project_id, task_id, pointer_role, snapshot_id,
                   pointer_version, created_at, updated_at
               ) VALUES (?, ?, ?, 'TASK_LATEST_WORKING', ?, 0, ?, ?)""",
            (pointer_id, project.project_id, task_id, snapshots[1], TIMESTAMP, TIMESTAMP),
        )

    assert not hasattr(service, "compare_and_swap_pointer")
    with open_existing_registry(home=isolated_kb_home, now=utc_now, event_id=new_id) as conn:
        assert conn is not None
        with immediate_registry_transaction(conn):
            assert (
                _compare_and_swap_pointer(
                    conn,
                    pointer_id,
                    expected_pointer_version=0,
                    snapshot_id=snapshots[2],
                    updated_at=TIMESTAMP,
                )
                == 1
            )
        with (
            pytest.raises(RegistryOperationError, match="compare-and-swap"),
            immediate_registry_transaction(conn),
        ):
            _compare_and_swap_pointer(
                conn,
                pointer_id,
                expected_pointer_version=0,
                snapshot_id=snapshots[1],
                updated_at=TIMESTAMP,
            )
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="generation identity"):
            conn.execute(
                """UPDATE snapshot_generations
                   SET file_sha256 = ?, row_version = row_version + 1, updated_at = ?
                   WHERE snapshot_id = ?""",
                ("f" * 64, TIMESTAMP, snapshots[2]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="direction"):
            conn.execute(
                """UPDATE managed_pointers
                   SET snapshot_id = ?, pointer_version = pointer_version + 1, updated_at = ?
                   WHERE pointer_id = ?""",
                (snapshots[1], TIMESTAMP, pointer_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="remain available"):
            conn.execute(
                """UPDATE snapshot_generations
                   SET generation_state = 'QUARANTINED', row_version = row_version + 1,
                       updated_at = ?
                   WHERE snapshot_id = ?""",
                (TIMESTAMP, snapshots[2]),
            )


def test_relink_and_unregister_reject_open_or_retained_lifecycle_history(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    storage = Path(project.storage_path)
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        before = conn.execute(
            """SELECT last_status, last_indexed_at, last_git_commit,
                      snapshot_binding_generation
               FROM projects WHERE project_id = ?""",
            (project.project_id,),
        ).fetchone()
        _insert_task(conn, project, task_id="9" * 32, state="ACTIVE")
        with pytest.raises(sqlite3.IntegrityError, match="open lifecycle work"):
            conn.execute(
                """UPDATE workspaces
                   SET workspace_binding_generation = ?, row_version = row_version + 1,
                       updated_at = ?
                   WHERE workspace_id = ?""",
                (new_id(), TIMESTAMP, project.workspace_id),
            )
        after = conn.execute(
            """SELECT last_status, last_indexed_at, last_git_commit,
                      snapshot_binding_generation
               FROM projects WHERE project_id = ?""",
            (project.project_id,),
        ).fetchone()
    assert after == before

    with pytest.raises(RegistryOperationError, match="open lifecycle work"):
        service.relink("repo", second_temp_git_repo)
    with pytest.raises(RegistryOperationError, match="cannot be hard-unregistered"):
        service.unregister("repo", yes=True)
    preserved = service.find_project_by_name("repo")
    assert preserved is not None
    assert preserved.repo_root == project.repo_root
    assert preserved.repo_binding_generation == project.repo_binding_generation
    assert storage.is_dir()


def test_unregister_database_failure_after_storage_preparation_is_compensated(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    storage_path = Path(project.storage_path)
    original_log_event = RegistryService._log_event

    def fail_unregister_event(
        self: RegistryService,
        conn: sqlite3.Connection,
        *,
        project_id: str | None,
        project_name: str | None,
        event_type: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        if event_type == "unregister":
            raise sqlite3.OperationalError("injected final registry failure")
        original_log_event(
            self,
            conn,
            project_id=project_id,
            project_name=project_name,
            event_type=event_type,
            message=message,
            details=details,
        )

    monkeypatch.setattr(RegistryService, "_log_event", fail_unregister_event)

    with pytest.raises(RegistryOperationError, match="Registry operation failed"):
        service.unregister("repo", yes=True)

    preserved = service.find_project_by_name("repo")
    assert preserved is not None
    assert preserved.workspace_state == "ACTIVE"
    assert storage_path.is_dir()
    assert list(storage_path.parent.glob(f".{project.project_id}.unregister-*")) == []


def test_unregister_stale_workspace_version_restores_storage_and_stays_fail_closed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    storage_path = Path(project.storage_path)
    original_prepare = registry_service._prepare_project_storage_removal

    def prepare_then_advance_version(
        path: Path,
        *,
        project_id: str,
    ) -> object:
        prepared = original_prepare(path, project_id=project_id)
        with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
            conn.execute(
                """UPDATE workspaces
                   SET workspace_state = 'RELINK_REQUIRED', row_version = row_version + 1,
                       updated_at = ? WHERE workspace_id = ?""",
                (TIMESTAMP, project.workspace_id),
            )
        return prepared

    monkeypatch.setattr(
        registry_service,
        "_prepare_project_storage_removal",
        prepare_then_advance_version,
    )

    with pytest.raises(RegistryOperationError, match="remains fail-closed"):
        service.unregister("repo", yes=True)

    preserved = service.find_project_by_name("repo")
    assert preserved is not None
    assert preserved.workspace_state == "RELINK_REQUIRED"
    assert storage_path.is_dir()
    assert list(storage_path.parent.glob(f".{project.project_id}.unregister-*")) == []


def test_unregister_rechecks_lifecycle_history_after_storage_preparation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    storage_path = Path(project.storage_path)
    original_prepare = registry_service._prepare_project_storage_removal

    def prepare_then_add_history(
        path: Path,
        *,
        project_id: str,
    ) -> object:
        prepared = original_prepare(path, project_id=project_id)
        with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DROP TRIGGER lifecycle_tasks_initial_state_guard")
            _insert_task_row(conn, project, task_id="f" * 32, state="CLOSED")
            conn.execute(registry_schema.TRIGGER_SQL["lifecycle_tasks_initial_state_guard"])
        return prepared

    monkeypatch.setattr(
        registry_service,
        "_prepare_project_storage_removal",
        prepare_then_add_history,
    )

    with pytest.raises(RegistryOperationError, match="cannot be hard-unregistered"):
        service.unregister("repo", yes=True)

    preserved = service.find_project_by_name("repo")
    assert preserved is not None
    assert preserved.workspace_state == "ACTIVE"
    assert storage_path.is_dir()
    with sqlite3.connect(isolated_kb_home / "registry.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM lifecycle_tasks").fetchone()[0] == 1


def test_unregister_recursive_delete_failure_leaves_no_usable_registry_row(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    storage_path = Path(project.storage_path)

    def fail_recursive_delete(_path: Path, *, home: Path) -> dict[str, object]:
        raise RegistryOperationError(
            "injected recursive delete failure",
            details={"home": str(home)},
        )

    monkeypatch.setattr(registry_service, "remove_project_storage", fail_recursive_delete)

    with pytest.raises(RegistryOperationError, match="was unregistered"):
        service.unregister("repo", yes=True)

    assert service.find_project_by_name("repo") is None
    assert not storage_path.exists()
    assert len(list(storage_path.parent.glob(f".{project.project_id}.unregister-*"))) == 1


def test_unregister_post_removal_database_failure_has_no_usable_registry_row(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    storage_path = Path(project.storage_path)

    def fail_storage_deleted_event(_project: ProjectRecord) -> None:
        raise sqlite3.OperationalError("injected post-removal database failure")

    monkeypatch.setattr(service, "_record_storage_deleted_event", fail_storage_deleted_event)

    with pytest.raises(RegistryOperationError, match="audit logging failed"):
        service.unregister("repo", yes=True)

    assert service.find_project_by_name("repo") is None
    assert not storage_path.exists()
    assert list(storage_path.parent.glob(f".{project.project_id}.unregister-*")) == []


def test_closed_history_stays_on_old_binding_after_workspace_relink(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    old_binding = project.repo_binding_generation
    task_id = "d" * 32
    registry_path = isolated_kb_home / "registry.sqlite"
    with sqlite3.connect(registry_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _insert_task(conn, project, task_id=task_id, state="CLOSED")

    relinked = service.relink("repo", second_temp_git_repo).project

    assert relinked is not None
    assert relinked.workspace_id == project.workspace_id
    assert relinked.repo_binding_generation != old_binding
    with sqlite3.connect(registry_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        retained_binding = conn.execute(
            "SELECT workspace_binding_generation FROM lifecycle_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        assert retained_binding == old_binding
        with pytest.raises(sqlite3.IntegrityError, match="active workspace binding"):
            _insert_task(conn, project, task_id="e" * 32, state="ACTIVE")


def test_windows_case_alias_current_directory_resolves_authoritative_workspace(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    assert os.name == "nt"
    service = RegistryService(home=isolated_kb_home)
    project = service.register("repo", temp_git_repo).project
    assert project is not None
    case_alias = Path(str(temp_git_repo).swapcase())

    by_root = service.find_project_by_repo_root(case_alias)
    status = ProjectStatusService(
        home=isolated_kb_home,
        working_directory=case_alias,
    ).status()

    assert by_root is not None
    assert by_root.project_id == project.project_id
    assert by_root.workspace_id == project.workspace_id
    assert status.project is not None
    assert status.project.project_id == project.project_id


def _create_v3_registry(
    home: Path,
    projects: list[tuple[str, Path]],
) -> tuple[Path, dict[str, dict[str, str]]]:
    home.mkdir(parents=True)
    registry_path = home / "registry.sqlite"
    expected: dict[str, dict[str, str]] = {}
    with sqlite3.connect(registry_path) as conn:
        conn.executescript(PROJECTS_TABLE_SQL + REGISTRY_EVENTS_TABLE_SQL + META_TABLE_SQL)
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("schema_version", "3"),
                ("created_at", TIMESTAMP),
                ("tool_version", "0.1.0"),
            ],
        )
        for name, root in projects:
            project_id = new_id()
            binding = new_id()
            resolved = root.resolve()
            fingerprint = build_repository_fingerprint(resolved).to_json()
            storage = home / "projects" / project_id
            conn.execute(
                """INSERT INTO projects (
                       project_id, project_name, project_name_norm, repo_root, repo_root_norm,
                       storage_path, created_at, updated_at, last_status,
                       last_indexed_at, last_git_commit, repo_fingerprint_json,
                       repo_binding_generation, snapshot_binding_generation
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REGISTERED', NULL, NULL, ?, ?, ?)""",
                (
                    project_id,
                    name,
                    name.casefold(),
                    str(resolved),
                    normalize_repo_root(resolved),
                    str(storage),
                    TIMESTAMP,
                    TIMESTAMP,
                    fingerprint,
                    binding,
                    binding,
                ),
            )
            expected[project_id] = {
                "repo_root": str(resolved),
                "repo_root_norm": normalize_repo_root(resolved),
                "fingerprint": fingerprint,
                "binding": binding,
            }
    return registry_path, expected


def _insert_task(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    *,
    task_id: str,
    state: str,
    workspace_id: str | None = None,
    project_baseline_snapshot_id: str | None = None,
    project_baseline_pointer_version: int | None = None,
) -> None:
    _insert_task_row(
        conn,
        project,
        task_id=task_id,
        state="DRAFT",
        workspace_id=workspace_id,
        project_baseline_snapshot_id=project_baseline_snapshot_id,
        project_baseline_pointer_version=project_baseline_pointer_version,
    )
    for next_state in TASK_STATE_PATHS[state]:
        conn.execute(
            """UPDATE lifecycle_tasks
               SET task_state = ?, row_version = row_version + 1, updated_at = ?
               WHERE task_id = ?""",
            (next_state, TIMESTAMP, task_id),
        )


def _insert_task_row(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    *,
    task_id: str,
    state: str,
    workspace_id: str | None = None,
    project_baseline_snapshot_id: str | None = None,
    project_baseline_pointer_version: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO lifecycle_tasks (
               task_id, project_id, workspace_id, workspace_binding_generation,
               task_state, capture_contract_fingerprint, baseline_head_commit,
               baseline_head_ref, next_generation_sequence,
               project_baseline_snapshot_id, project_baseline_pointer_version,
               row_version, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, 'capture-contract', ?, 'main', 0, ?, ?, 0, ?, ?)""",
        (
            task_id,
            project.project_id,
            workspace_id or project.workspace_id,
            project.repo_binding_generation,
            state,
            "1" * 40,
            project_baseline_snapshot_id,
            project_baseline_pointer_version,
            TIMESTAMP,
            TIMESTAMP,
        ),
    )


def _insert_committed_operation(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    task_id: str,
    operation_id: str,
) -> None:
    _insert_operation(
        conn,
        project,
        operation_id=operation_id,
        phase="COMMITTED",
        task_id=task_id,
    )


def _insert_operation(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    *,
    operation_id: str,
    phase: str,
    task_id: str | None = None,
    reserved_snapshot_id: str | None = None,
    expected_task_version: int | None = None,
    expected_pointer_versions_json: str | None = None,
) -> None:
    _insert_operation_row(
        conn,
        project,
        operation_id=operation_id,
        phase="RESERVED",
        task_id=task_id,
        reserved_snapshot_id=reserved_snapshot_id,
        expected_task_version=expected_task_version,
        expected_pointer_versions_json=expected_pointer_versions_json,
    )
    for next_phase in OPERATION_PHASE_PATHS[phase]:
        conn.execute(
            """UPDATE lifecycle_operations
               SET operation_phase = ?, row_version = row_version + 1, updated_at = ?
               WHERE operation_id = ?""",
            (next_phase, TIMESTAMP, operation_id),
        )


def _insert_operation_row(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    *,
    operation_id: str,
    phase: str,
    task_id: str | None = None,
    reserved_snapshot_id: str | None = None,
    expected_task_version: int | None = None,
    expected_pointer_versions_json: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO lifecycle_operations (
               operation_id, idempotency_key, request_fingerprint,
               operation_kind, operation_phase, actor_context_json,
               project_id, workspace_id, workspace_binding_generation, task_id,
               reserved_snapshot_id, expected_task_version, expected_pointer_versions_json,
               row_version, created_at, updated_at
           ) VALUES (?, ?, ?, 'REFRESH_WORKING', ?, '{}', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            operation_id,
            f"key-{operation_id}",
            f"request-{operation_id}",
            phase,
            project.project_id,
            project.workspace_id,
            project.repo_binding_generation,
            task_id,
            reserved_snapshot_id,
            expected_task_version,
            expected_pointer_versions_json,
            TIMESTAMP,
            TIMESTAMP,
        ),
    )


def _insert_generation(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    task_id: str,
    operation_id: str,
    snapshot_id: str,
    sequence: int,
    parent_snapshot_id: str | None,
    *,
    purpose: str | None = None,
    state: str = "AVAILABLE",
) -> None:
    initial_state = state if state in INITIAL_GENERATION_STATES else "AVAILABLE"
    _insert_generation_row(
        conn,
        project,
        task_id,
        operation_id,
        snapshot_id,
        sequence,
        parent_snapshot_id,
        purpose=purpose,
        state=initial_state,
    )
    for next_state in GENERATION_STATE_PATHS[state]:
        conn.execute(
            """UPDATE snapshot_generations
               SET generation_state = ?, row_version = row_version + 1, updated_at = ?
               WHERE snapshot_id = ?""",
            (next_state, TIMESTAMP, snapshot_id),
        )


def _insert_generation_row(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    task_id: str,
    operation_id: str,
    snapshot_id: str,
    sequence: int,
    parent_snapshot_id: str | None,
    *,
    purpose: str | None = None,
    state: str = "AVAILABLE",
) -> None:
    resolved_purpose = purpose or ("TASK_BASELINE" if sequence == 0 else "TASK_WORKING")
    conn.execute(
        """INSERT INTO snapshot_generations (
               snapshot_id, project_id, workspace_id, workspace_binding_generation,
               task_id, generation_sequence, parent_snapshot_id, capture_purpose,
               origin_operation_id, capture_contract_fingerprint,
               repository_evidence_json, relative_storage_path, storage_layout_version,
               file_size, file_sha256, truth_claim, generation_state, row_version,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'capture-contract', '{}', ?, 1,
                     1, ?, 'CAPTURED_STABLE', ?, 0, ?, ?)""",
        (
            snapshot_id,
            project.project_id,
            project.workspace_id,
            project.repo_binding_generation,
            task_id,
            sequence,
            parent_snapshot_id,
            resolved_purpose,
            operation_id,
            f"generations/{snapshot_id}.sqlite",
            f"{sequence + 1:x}" * 64,
            state,
            TIMESTAMP,
            TIMESTAMP,
        ),
    )


def _insert_pointer(
    conn: sqlite3.Connection,
    project: ProjectRecord,
    *,
    pointer_id: str,
    pointer_role: str,
    snapshot_id: str,
    task_id: str | None,
) -> None:
    conn.execute(
        """INSERT INTO managed_pointers (
               pointer_id, project_id, task_id, pointer_role, snapshot_id,
               pointer_version, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            pointer_id,
            project.project_id,
            task_id,
            pointer_role,
            snapshot_id,
            TIMESTAMP,
            TIMESTAMP,
        ),
    )
