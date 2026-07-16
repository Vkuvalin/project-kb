"""SQLite registry schema and explicit monotonic migrations."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

SCHEMA_VERSION = "4"
V3_SCHEMA_VERSION = "3"


PROJECTS_TABLE_SQL = """
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
    last_git_commit TEXT NULL,
    repo_fingerprint_json TEXT NULL,
    repo_binding_generation TEXT NOT NULL,
    snapshot_binding_generation TEXT NOT NULL
);
"""

REGISTRY_EVENTS_TABLE_SQL = """
CREATE TABLE registry_events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NULL,
    project_name TEXT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NULL,
    created_at TEXT NOT NULL
);
"""

META_TABLE_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

WORKSPACES_TABLE_SQL = """
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY
        CHECK (length(workspace_id) = 32 AND workspace_id NOT GLOB '*[^0-9a-f]*'),
    project_id TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    workspace_root_norm TEXT NOT NULL UNIQUE,
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('PRIMARY', 'LINKED')),
    repository_fingerprint_json TEXT NULL,
    workspace_binding_generation TEXT NOT NULL
        CHECK (
            length(workspace_binding_generation) = 32
            AND workspace_binding_generation NOT GLOB '*[^0-9a-f]*'
        ),
    workspace_state TEXT NOT NULL
        CHECK (workspace_state IN ('ACTIVE', 'UNAVAILABLE', 'RELINK_REQUIRED', 'RETIRED')),
    row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, project_id),
    UNIQUE (workspace_id, project_id, workspace_binding_generation),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
"""

LIFECYCLE_TASKS_TABLE_SQL = """
CREATE TABLE lifecycle_tasks (
    task_id TEXT PRIMARY KEY
        CHECK (length(task_id) = 32 AND task_id NOT GLOB '*[^0-9a-f]*'),
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_binding_generation TEXT NOT NULL
        CHECK (
            length(workspace_binding_generation) = 32
            AND workspace_binding_generation NOT GLOB '*[^0-9a-f]*'
        ),
    task_state TEXT NOT NULL
        CHECK (task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED', 'ACCEPTED', 'ABANDONED', 'CLOSED')),
    capture_contract_fingerprint TEXT NOT NULL,
    baseline_head_commit TEXT NOT NULL,
    baseline_head_ref TEXT NULL,
    next_generation_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_generation_sequence >= 0),
    project_baseline_snapshot_id TEXT NULL,
    project_baseline_pointer_version INTEGER NULL
        CHECK (project_baseline_pointer_version IS NULL OR project_baseline_pointer_version >= 0),
    row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, project_id, workspace_id, workspace_binding_generation),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, project_id)
        REFERENCES workspaces(workspace_id, project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
"""

LIFECYCLE_OPERATIONS_TABLE_SQL = """
CREATE TABLE lifecycle_operations (
    operation_id TEXT PRIMARY KEY
        CHECK (length(operation_id) = 32 AND operation_id NOT GLOB '*[^0-9a-f]*'),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    operation_kind TEXT NOT NULL
        CHECK (
            operation_kind IN (
                'BEGIN_TASK', 'REFRESH_WORKING', 'ACCEPT_TASK', 'ABANDON_TASK',
                'CLOSE_TASK', 'DELETE_GENERATION', 'RECOVER', 'RECONCILE'
            )
        ),
    operation_phase TEXT NOT NULL
        CHECK (
            operation_phase IN (
                'RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'COMMITTED',
                'FAILED', 'RECOVERY_REQUIRED'
            )
        ),
    actor_context_json TEXT NOT NULL,
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_binding_generation TEXT NOT NULL
        CHECK (
            length(workspace_binding_generation) = 32
            AND workspace_binding_generation NOT GLOB '*[^0-9a-f]*'
        ),
    task_id TEXT NULL,
    reserved_snapshot_id TEXT NULL,
    reserved_generation_sequence INTEGER NULL
        CHECK (reserved_generation_sequence IS NULL OR reserved_generation_sequence >= 0),
    expected_task_version INTEGER NULL
        CHECK (expected_task_version IS NULL OR expected_task_version >= 0),
    expected_pointer_versions_json TEXT NULL,
    lease_owner TEXT NULL,
    lease_expires_at TEXT NULL,
    managed_temp_path TEXT NULL,
    managed_final_path TEXT NULL,
    failure_json TEXT NULL,
    row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, project_id)
        REFERENCES workspaces(workspace_id, project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (task_id, project_id, workspace_id, workspace_binding_generation)
        REFERENCES lifecycle_tasks(task_id, project_id, workspace_id, workspace_binding_generation)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
"""

SNAPSHOT_GENERATIONS_TABLE_SQL = """
CREATE TABLE snapshot_generations (
    snapshot_id TEXT PRIMARY KEY
        CHECK (length(snapshot_id) = 32 AND snapshot_id NOT GLOB '*[^0-9a-f]*'),
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_binding_generation TEXT NOT NULL
        CHECK (
            length(workspace_binding_generation) = 32
            AND workspace_binding_generation NOT GLOB '*[^0-9a-f]*'
        ),
    task_id TEXT NOT NULL,
    generation_sequence INTEGER NOT NULL CHECK (generation_sequence >= 0),
    parent_snapshot_id TEXT NULL,
    capture_purpose TEXT NOT NULL
        CHECK (capture_purpose IN ('TASK_BASELINE', 'TASK_WORKING', 'TASK_FINAL')),
    origin_operation_id TEXT NOT NULL,
    capture_contract_fingerprint TEXT NOT NULL,
    repository_evidence_json TEXT NOT NULL,
    relative_storage_path TEXT NOT NULL CHECK (length(relative_storage_path) > 0),
    storage_layout_version INTEGER NOT NULL CHECK (storage_layout_version > 0),
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    file_sha256 TEXT NOT NULL
        CHECK (length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'),
    truth_claim TEXT NOT NULL CHECK (truth_claim = 'CAPTURED_STABLE'),
    generation_state TEXT NOT NULL
        CHECK (
            generation_state IN (
                'AVAILABLE', 'ORPHANED', 'QUARANTINED', 'PENDING_DELETE', 'DELETED'
            )
        ),
    row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, generation_sequence),
    UNIQUE (snapshot_id, project_id),
    UNIQUE (snapshot_id, task_id),
    CHECK (
        (capture_purpose = 'TASK_BASELINE'
            AND generation_sequence = 0 AND parent_snapshot_id IS NULL)
        OR
        (capture_purpose IN ('TASK_WORKING', 'TASK_FINAL')
            AND generation_sequence > 0 AND parent_snapshot_id IS NOT NULL)
    ),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (task_id, project_id, workspace_id, workspace_binding_generation)
        REFERENCES lifecycle_tasks(task_id, project_id, workspace_id, workspace_binding_generation)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (origin_operation_id) REFERENCES lifecycle_operations(operation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (parent_snapshot_id, task_id)
        REFERENCES snapshot_generations(snapshot_id, task_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
"""

MANAGED_POINTERS_TABLE_SQL = """
CREATE TABLE managed_pointers (
    pointer_id TEXT PRIMARY KEY
        CHECK (length(pointer_id) = 32 AND pointer_id NOT GLOB '*[^0-9a-f]*'),
    project_id TEXT NOT NULL,
    task_id TEXT NULL,
    pointer_role TEXT NOT NULL
        CHECK (
            pointer_role IN (
                'TASK_BASELINE', 'TASK_LATEST_WORKING', 'TASK_FINAL', 'PROJECT_BASELINE'
            )
        ),
    snapshot_id TEXT NOT NULL,
    pointer_version INTEGER NOT NULL DEFAULT 0 CHECK (pointer_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (pointer_role = 'PROJECT_BASELINE' AND task_id IS NULL)
        OR
        (pointer_role IN ('TASK_BASELINE', 'TASK_LATEST_WORKING', 'TASK_FINAL')
            AND task_id IS NOT NULL)
    ),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (task_id) REFERENCES lifecycle_tasks(task_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (snapshot_id, project_id)
        REFERENCES snapshot_generations(snapshot_id, project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (snapshot_id, task_id)
        REFERENCES snapshot_generations(snapshot_id, task_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
"""


INDEX_SQL: dict[str, str] = {
    "ux_workspaces_primary_project": """
        CREATE UNIQUE INDEX ux_workspaces_primary_project
        ON workspaces(project_id) WHERE workspace_kind = 'PRIMARY'
    """,
    "idx_workspaces_project": """
        CREATE INDEX idx_workspaces_project ON workspaces(project_id, workspace_kind)
    """,
    "ux_tasks_open_workspace_binding": """
        CREATE UNIQUE INDEX ux_tasks_open_workspace_binding
        ON lifecycle_tasks(workspace_id, workspace_binding_generation)
        WHERE task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
    """,
    "idx_tasks_project_workspace": """
        CREATE INDEX idx_tasks_project_workspace
        ON lifecycle_tasks(project_id, workspace_id, created_at)
    """,
    "idx_operations_scope": """
        CREATE INDEX idx_operations_scope
        ON lifecycle_operations(project_id, workspace_id, task_id, created_at)
    """,
    "ux_operations_active_workspace_binding": """
        CREATE UNIQUE INDEX ux_operations_active_workspace_binding
        ON lifecycle_operations(workspace_id, workspace_binding_generation)
        WHERE operation_phase IN ('RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED')
    """,
    "idx_generations_lineage": """
        CREATE INDEX idx_generations_lineage
        ON snapshot_generations(task_id, generation_sequence)
    """,
    "ux_pointers_project_baseline": """
        CREATE UNIQUE INDEX ux_pointers_project_baseline
        ON managed_pointers(project_id)
        WHERE pointer_role = 'PROJECT_BASELINE'
    """,
    "ux_pointers_task_role": """
        CREATE UNIQUE INDEX ux_pointers_task_role
        ON managed_pointers(task_id, pointer_role)
        WHERE task_id IS NOT NULL
    """,
}


TRIGGER_SQL: dict[str, str] = {
    "projects_create_primary_workspace": """
        CREATE TRIGGER projects_create_primary_workspace
        AFTER INSERT ON projects
        BEGIN
            INSERT INTO workspaces (
                workspace_id, project_id, workspace_root, workspace_root_norm,
                workspace_kind, repository_fingerprint_json,
                workspace_binding_generation, workspace_state, row_version,
                created_at, updated_at
            )
            VALUES (
                lower(hex(randomblob(16))), NEW.project_id, NEW.repo_root, NEW.repo_root_norm,
                'PRIMARY', NEW.repo_fingerprint_json, NEW.repo_binding_generation,
                'ACTIVE', 0, NEW.created_at, NEW.updated_at
            );
        END
    """,
    "projects_workspace_projection_guard": """
        CREATE TRIGGER projects_workspace_projection_guard
        BEFORE UPDATE OF repo_root, repo_root_norm, repo_fingerprint_json, repo_binding_generation
        ON projects
        WHEN NOT EXISTS (
            SELECT 1 FROM workspaces
            WHERE project_id = NEW.project_id
              AND workspace_kind = 'PRIMARY'
              AND workspace_root IS NEW.repo_root
              AND workspace_root_norm IS NEW.repo_root_norm
              AND repository_fingerprint_json IS NEW.repo_fingerprint_json
              AND workspace_binding_generation IS NEW.repo_binding_generation
        )
        BEGIN
            SELECT RAISE(ABORT, 'projects workspace compatibility projection drift');
        END
    """,
    "projects_physical_authority_delete_guard": """
        CREATE TRIGGER projects_physical_authority_delete_guard
        BEFORE DELETE ON projects
        WHEN EXISTS (SELECT 1 FROM workspaces WHERE project_id = OLD.project_id)
        BEGIN
            SELECT RAISE(ABORT, 'project physical workspace authority must be removed first');
        END
    """,
    "workspaces_project_primary_projection": """
        CREATE TRIGGER workspaces_project_primary_projection
        AFTER UPDATE OF workspace_root, workspace_root_norm, repository_fingerprint_json,
                        workspace_binding_generation
        ON workspaces
        WHEN NEW.workspace_kind = 'PRIMARY'
        BEGIN
            UPDATE projects
            SET repo_root = NEW.workspace_root,
                repo_root_norm = NEW.workspace_root_norm,
                repo_fingerprint_json = NEW.repository_fingerprint_json,
                repo_binding_generation = NEW.workspace_binding_generation,
                updated_at = NEW.updated_at
            WHERE project_id = NEW.project_id;
        END
    """,
    "workspaces_update_guard": """
        CREATE TRIGGER workspaces_update_guard
        BEFORE UPDATE ON workspaces
        WHEN NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.project_id IS NOT OLD.project_id
          OR NEW.workspace_kind IS NOT OLD.workspace_kind
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version != OLD.row_version + 1
          OR OLD.workspace_state = 'RETIRED'
          OR (NEW.workspace_state = 'RETIRED'
              AND (
                  NEW.workspace_root IS NOT OLD.workspace_root
                  OR NEW.workspace_root_norm IS NOT OLD.workspace_root_norm
                  OR NEW.repository_fingerprint_json IS NOT OLD.repository_fingerprint_json
                  OR NEW.workspace_binding_generation IS NOT OLD.workspace_binding_generation
              ))
        BEGIN
            SELECT RAISE(ABORT, 'workspace immutable ownership or row version violation');
        END
    """,
    "workspaces_open_lifecycle_relink_guard": """
        CREATE TRIGGER workspaces_open_lifecycle_relink_guard
        BEFORE UPDATE OF workspace_root, workspace_root_norm, repository_fingerprint_json,
                         workspace_binding_generation
        ON workspaces
        WHEN (
                NEW.workspace_root IS NOT OLD.workspace_root
                OR NEW.workspace_root_norm IS NOT OLD.workspace_root_norm
                OR NEW.repository_fingerprint_json IS NOT OLD.repository_fingerprint_json
                OR NEW.workspace_binding_generation IS NOT OLD.workspace_binding_generation
             )
          AND (
              EXISTS (
                  SELECT 1 FROM lifecycle_tasks
                  WHERE workspace_id = OLD.workspace_id
                    AND workspace_binding_generation = OLD.workspace_binding_generation
                    AND task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
              )
              OR EXISTS (
                  SELECT 1 FROM lifecycle_operations
                  WHERE workspace_id = OLD.workspace_id
                    AND workspace_binding_generation = OLD.workspace_binding_generation
                    AND operation_phase IN (
                        'RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED'
                    )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'open lifecycle work prevents workspace relink');
        END
    """,
    "workspaces_open_lifecycle_retire_guard": """
        CREATE TRIGGER workspaces_open_lifecycle_retire_guard
        BEFORE UPDATE OF workspace_state ON workspaces
        WHEN NEW.workspace_state = 'RETIRED'
          AND OLD.workspace_state != 'RETIRED'
          AND (
              EXISTS (
                  SELECT 1 FROM lifecycle_tasks
                  WHERE workspace_id = OLD.workspace_id
                    AND workspace_binding_generation = OLD.workspace_binding_generation
                    AND task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
              )
              OR EXISTS (
                  SELECT 1 FROM lifecycle_operations
                  WHERE workspace_id = OLD.workspace_id
                    AND workspace_binding_generation = OLD.workspace_binding_generation
                    AND operation_phase IN (
                        'RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED'
                    )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'open lifecycle work prevents workspace retirement');
        END
    """,
    "workspaces_primary_delete_project": """
        CREATE TRIGGER workspaces_primary_delete_project
        AFTER DELETE ON workspaces
        WHEN OLD.workspace_kind = 'PRIMARY'
        BEGIN
            DELETE FROM projects WHERE project_id = OLD.project_id;
        END
    """,
    "workspaces_lifecycle_delete_guard": """
        CREATE TRIGGER workspaces_lifecycle_delete_guard
        BEFORE DELETE ON workspaces
        WHEN EXISTS (
                SELECT 1 FROM lifecycle_tasks WHERE workspace_id = OLD.workspace_id
             )
          OR EXISTS (
                SELECT 1 FROM lifecycle_operations WHERE workspace_id = OLD.workspace_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'lifecycle-bearing workspace cannot be deleted');
        END
    """,
    "lifecycle_tasks_update_guard": """
        CREATE TRIGGER lifecycle_tasks_update_guard
        BEFORE UPDATE ON lifecycle_tasks
        WHEN NEW.task_id IS NOT OLD.task_id
          OR NEW.project_id IS NOT OLD.project_id
          OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.workspace_binding_generation IS NOT OLD.workspace_binding_generation
          OR NEW.capture_contract_fingerprint IS NOT OLD.capture_contract_fingerprint
          OR NEW.baseline_head_commit IS NOT OLD.baseline_head_commit
          OR NEW.baseline_head_ref IS NOT OLD.baseline_head_ref
          OR NEW.project_baseline_snapshot_id IS NOT OLD.project_baseline_snapshot_id
          OR NEW.project_baseline_pointer_version IS NOT OLD.project_baseline_pointer_version
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version != OLD.row_version + 1
          OR (NEW.task_state IN ('ACCEPTED', 'ABANDONED', 'CLOSED')
              AND NEW.next_generation_sequence IS NOT OLD.next_generation_sequence)
          OR NOT (
              (OLD.task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
                  AND NEW.task_state = OLD.task_state)
              OR (OLD.task_state = 'DRAFT' AND NEW.task_state IN ('ACTIVE', 'BLOCKED', 'ABANDONED'))
              OR (OLD.task_state = 'ACTIVE'
                  AND NEW.task_state IN ('BLOCKED', 'ACCEPTED', 'ABANDONED'))
              OR (OLD.task_state = 'BLOCKED' AND NEW.task_state IN ('ACTIVE', 'ABANDONED'))
              OR (OLD.task_state IN ('ACCEPTED', 'ABANDONED') AND NEW.task_state = 'CLOSED')
          )
        BEGIN
            SELECT RAISE(ABORT, 'task ownership, transition, or row version violation');
        END
    """,
    "lifecycle_tasks_initial_state_guard": """
        CREATE TRIGGER lifecycle_tasks_initial_state_guard
        BEFORE INSERT ON lifecycle_tasks
        WHEN NEW.task_state != 'DRAFT'
        BEGIN
            SELECT RAISE(ABORT, 'lifecycle task must start in DRAFT');
        END
    """,
    "lifecycle_tasks_open_binding_insert_guard": """
        CREATE TRIGGER lifecycle_tasks_open_binding_insert_guard
        BEFORE INSERT ON lifecycle_tasks
        WHEN NEW.task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
          AND NOT EXISTS (
              SELECT 1 FROM workspaces
              WHERE workspace_id = NEW.workspace_id
                AND project_id = NEW.project_id
                AND workspace_binding_generation = NEW.workspace_binding_generation
                AND workspace_state = 'ACTIVE'
          )
        BEGIN
            SELECT RAISE(ABORT, 'open task must own the active workspace binding');
        END
    """,
    "lifecycle_tasks_open_binding_update_guard": """
        CREATE TRIGGER lifecycle_tasks_open_binding_update_guard
        BEFORE UPDATE OF task_state ON lifecycle_tasks
        WHEN NEW.task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
          AND NOT EXISTS (
              SELECT 1 FROM workspaces
              WHERE workspace_id = NEW.workspace_id
                AND project_id = NEW.project_id
                AND workspace_binding_generation = NEW.workspace_binding_generation
                AND workspace_state = 'ACTIVE'
          )
        BEGIN
            SELECT RAISE(ABORT, 'open task must own the active workspace binding');
        END
    """,
    "lifecycle_tasks_delete_guard": """
        CREATE TRIGGER lifecycle_tasks_delete_guard
        BEFORE DELETE ON lifecycle_tasks
        BEGIN
            SELECT RAISE(ABORT, 'lifecycle task history cannot be deleted');
        END
    """,
    "lifecycle_operations_update_guard": """
        CREATE TRIGGER lifecycle_operations_update_guard
        BEFORE UPDATE ON lifecycle_operations
        WHEN NEW.operation_id IS NOT OLD.operation_id
          OR NEW.idempotency_key IS NOT OLD.idempotency_key
          OR NEW.request_fingerprint IS NOT OLD.request_fingerprint
          OR NEW.operation_kind IS NOT OLD.operation_kind
          OR NEW.actor_context_json IS NOT OLD.actor_context_json
          OR NEW.project_id IS NOT OLD.project_id
          OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.workspace_binding_generation IS NOT OLD.workspace_binding_generation
          OR NEW.task_id IS NOT OLD.task_id
          OR NEW.reserved_snapshot_id IS NOT OLD.reserved_snapshot_id
          OR NEW.reserved_generation_sequence IS NOT OLD.reserved_generation_sequence
          OR NEW.expected_task_version IS NOT OLD.expected_task_version
          OR NEW.expected_pointer_versions_json IS NOT OLD.expected_pointer_versions_json
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version != OLD.row_version + 1
          OR NOT (
              (OLD.operation_phase = 'RESERVED'
                  AND NEW.operation_phase IN (
                      'RESERVED', 'BUILDING', 'FAILED', 'RECOVERY_REQUIRED'
                  ))
              OR (OLD.operation_phase = 'BUILDING'
                  AND NEW.operation_phase IN (
                      'BUILDING', 'FILE_PUBLISHED', 'FAILED', 'RECOVERY_REQUIRED'
                  ))
              OR (OLD.operation_phase = 'FILE_PUBLISHED'
                  AND NEW.operation_phase IN (
                      'FILE_PUBLISHED', 'COMMITTED', 'RECOVERY_REQUIRED'
                  ))
              OR (OLD.operation_phase = 'RECOVERY_REQUIRED'
                  AND NEW.operation_phase IN ('RECOVERY_REQUIRED', 'COMMITTED', 'FAILED'))
          )
        BEGIN
            SELECT RAISE(ABORT, 'operation ownership, transition, or row version violation');
        END
    """,
    "lifecycle_operations_initial_phase_guard": """
        CREATE TRIGGER lifecycle_operations_initial_phase_guard
        BEFORE INSERT ON lifecycle_operations
        WHEN NEW.operation_phase != 'RESERVED'
        BEGIN
            SELECT RAISE(ABORT, 'lifecycle operation must start in RESERVED');
        END
    """,
    "lifecycle_operations_active_binding_insert_guard": """
        CREATE TRIGGER lifecycle_operations_active_binding_insert_guard
        BEFORE INSERT ON lifecycle_operations
        WHEN NEW.operation_phase IN ('RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED')
          AND NOT EXISTS (
              SELECT 1 FROM workspaces
              WHERE workspace_id = NEW.workspace_id
                AND project_id = NEW.project_id
                AND workspace_binding_generation = NEW.workspace_binding_generation
                AND workspace_state = 'ACTIVE'
          )
        BEGIN
            SELECT RAISE(ABORT, 'active operation must own the active workspace binding');
        END
    """,
    "lifecycle_operations_active_binding_update_guard": """
        CREATE TRIGGER lifecycle_operations_active_binding_update_guard
        BEFORE UPDATE ON lifecycle_operations
        WHEN NEW.operation_phase IN ('RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED')
          AND NOT EXISTS (
              SELECT 1 FROM workspaces
              WHERE workspace_id = NEW.workspace_id
                AND project_id = NEW.project_id
                AND workspace_binding_generation = NEW.workspace_binding_generation
                AND workspace_state = 'ACTIVE'
          )
        BEGIN
            SELECT RAISE(ABORT, 'active operation must own the active workspace binding');
        END
    """,
    "lifecycle_operations_delete_guard": """
        CREATE TRIGGER lifecycle_operations_delete_guard
        BEFORE DELETE ON lifecycle_operations
        BEGIN
            SELECT RAISE(ABORT, 'lifecycle operation history cannot be deleted');
        END
    """,
    "snapshot_generations_update_guard": """
        CREATE TRIGGER snapshot_generations_update_guard
        BEFORE UPDATE ON snapshot_generations
        WHEN NEW.snapshot_id IS NOT OLD.snapshot_id
          OR NEW.project_id IS NOT OLD.project_id
          OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.workspace_binding_generation IS NOT OLD.workspace_binding_generation
          OR NEW.task_id IS NOT OLD.task_id
          OR NEW.generation_sequence IS NOT OLD.generation_sequence
          OR NEW.parent_snapshot_id IS NOT OLD.parent_snapshot_id
          OR NEW.capture_purpose IS NOT OLD.capture_purpose
          OR NEW.origin_operation_id IS NOT OLD.origin_operation_id
          OR NEW.capture_contract_fingerprint IS NOT OLD.capture_contract_fingerprint
          OR NEW.repository_evidence_json IS NOT OLD.repository_evidence_json
          OR NEW.relative_storage_path IS NOT OLD.relative_storage_path
          OR NEW.storage_layout_version IS NOT OLD.storage_layout_version
          OR NEW.file_size IS NOT OLD.file_size
          OR NEW.file_sha256 IS NOT OLD.file_sha256
          OR NEW.truth_claim IS NOT OLD.truth_claim
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version != OLD.row_version + 1
          OR NOT (
              (OLD.generation_state = 'AVAILABLE'
                  AND NEW.generation_state IN ('ORPHANED', 'QUARANTINED', 'PENDING_DELETE'))
              OR (OLD.generation_state = 'ORPHANED'
                  AND NEW.generation_state IN ('QUARANTINED', 'PENDING_DELETE'))
              OR (OLD.generation_state = 'QUARANTINED'
                  AND NEW.generation_state = 'PENDING_DELETE')
              OR (OLD.generation_state = 'PENDING_DELETE'
                  AND NEW.generation_state = 'DELETED')
          )
        BEGIN
            SELECT RAISE(ABORT, 'generation identity, transition, or row version violation');
        END
    """,
    "snapshot_generations_initial_state_guard": """
        CREATE TRIGGER snapshot_generations_initial_state_guard
        BEFORE INSERT ON snapshot_generations
        WHEN NEW.generation_state NOT IN ('AVAILABLE', 'ORPHANED', 'QUARANTINED')
        BEGIN
            SELECT RAISE(ABORT, 'snapshot generation has an invalid initial state');
        END
    """,
    "snapshot_generations_cleanup_task_guard": """
        CREATE TRIGGER snapshot_generations_cleanup_task_guard
        BEFORE UPDATE OF generation_state ON snapshot_generations
        WHEN NEW.generation_state IN ('PENDING_DELETE', 'DELETED')
          AND NOT EXISTS (
              SELECT 1 FROM lifecycle_tasks
              WHERE task_id = OLD.task_id
                AND project_id = OLD.project_id
                AND task_state = 'CLOSED'
          )
        BEGIN
            SELECT RAISE(ABORT, 'generation cleanup requires a CLOSED owning task');
        END
    """,
    "snapshot_generations_unresolved_operation_guard": """
        CREATE TRIGGER snapshot_generations_unresolved_operation_guard
        BEFORE UPDATE OF generation_state ON snapshot_generations
        WHEN NEW.generation_state IN ('PENDING_DELETE', 'DELETED')
          AND EXISTS (
              SELECT 1 FROM lifecycle_operations
              WHERE reserved_snapshot_id = OLD.snapshot_id
                AND operation_phase IN (
                    'RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED'
                )
          )
        BEGIN
            SELECT RAISE(ABORT, 'unresolved operation protects generation from cleanup');
        END
    """,
    "snapshot_generations_live_pointer_guard": """
        CREATE TRIGGER snapshot_generations_live_pointer_guard
        BEFORE UPDATE OF generation_state ON snapshot_generations
        WHEN NEW.generation_state != 'AVAILABLE'
          AND EXISTS (
              SELECT 1
              FROM managed_pointers AS pointer
              LEFT JOIN lifecycle_tasks AS task ON task.task_id = pointer.task_id
              WHERE pointer.snapshot_id = OLD.snapshot_id
                AND (
                    pointer.pointer_role = 'PROJECT_BASELINE'
                    OR (pointer.task_id IS NOT NULL AND task.task_state != 'CLOSED')
                )
          )
        BEGIN
            SELECT RAISE(ABORT, 'live pointer target must remain available');
        END
    """,
    "snapshot_generations_delete_guard": """
        CREATE TRIGGER snapshot_generations_delete_guard
        BEFORE DELETE ON snapshot_generations
        BEGIN
            SELECT RAISE(ABORT, 'snapshot generation history cannot be deleted');
        END
    """,
    "managed_pointers_insert_guard": """
        CREATE TRIGGER managed_pointers_insert_guard
        BEFORE INSERT ON managed_pointers
        WHEN NOT EXISTS (
            SELECT 1 FROM snapshot_generations
            WHERE snapshot_id = NEW.snapshot_id
              AND project_id = NEW.project_id
              AND generation_state = 'AVAILABLE'
              AND (NEW.task_id IS NULL OR task_id = NEW.task_id)
              AND (
                  (NEW.pointer_role = 'TASK_BASELINE' AND capture_purpose = 'TASK_BASELINE')
                  OR (NEW.pointer_role = 'TASK_LATEST_WORKING'
                      AND capture_purpose = 'TASK_WORKING')
                  OR (NEW.pointer_role IN ('TASK_FINAL', 'PROJECT_BASELINE')
                      AND capture_purpose = 'TASK_FINAL')
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'pointer target must be an available owned generation');
        END
    """,
    "managed_pointers_update_guard": """
        CREATE TRIGGER managed_pointers_update_guard
        BEFORE UPDATE ON managed_pointers
        WHEN NEW.pointer_id IS NOT OLD.pointer_id
          OR NEW.project_id IS NOT OLD.project_id
          OR NEW.task_id IS NOT OLD.task_id
          OR NEW.pointer_role IS NOT OLD.pointer_role
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.pointer_version != OLD.pointer_version + 1
          OR NEW.snapshot_id IS OLD.snapshot_id
          OR (OLD.pointer_role IN ('TASK_BASELINE', 'TASK_FINAL')
              AND NEW.snapshot_id IS NOT OLD.snapshot_id)
          OR NOT EXISTS (
              SELECT 1 FROM snapshot_generations
              WHERE snapshot_id = NEW.snapshot_id
                AND project_id = NEW.project_id
                AND generation_state = 'AVAILABLE'
                AND (NEW.task_id IS NULL OR task_id = NEW.task_id)
                AND (
                    (NEW.pointer_role = 'TASK_BASELINE'
                        AND capture_purpose = 'TASK_BASELINE')
                    OR (NEW.pointer_role = 'TASK_LATEST_WORKING'
                        AND capture_purpose = 'TASK_WORKING')
                    OR (NEW.pointer_role IN ('TASK_FINAL', 'PROJECT_BASELINE')
                        AND capture_purpose = 'TASK_FINAL')
                )
          )
          OR (
              OLD.pointer_role = 'TASK_LATEST_WORKING'
              AND NEW.snapshot_id IS NOT OLD.snapshot_id
              AND (
                  SELECT generation_sequence FROM snapshot_generations
                  WHERE snapshot_id = NEW.snapshot_id
              ) <= (
                  SELECT generation_sequence FROM snapshot_generations
                  WHERE snapshot_id = OLD.snapshot_id
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'pointer ownership, direction, or version violation');
        END
    """,
    "managed_pointers_delete_guard": """
        CREATE TRIGGER managed_pointers_delete_guard
        BEFORE DELETE ON managed_pointers
        BEGIN
            SELECT RAISE(ABORT, 'managed pointer history cannot be deleted');
        END
    """,
}


BASE_REQUIRED_TABLES = {"projects", "registry_events", "meta"}
V4_REQUIRED_TABLES = BASE_REQUIRED_TABLES | {
    "workspaces",
    "lifecycle_tasks",
    "lifecycle_operations",
    "snapshot_generations",
    "managed_pointers",
}
V1_PROJECT_COLUMNS = {
    "project_id",
    "project_name",
    "project_name_norm",
    "repo_root",
    "repo_root_norm",
    "storage_path",
    "created_at",
    "updated_at",
    "last_status",
    "last_indexed_at",
    "last_git_commit",
}
V2_PROJECT_COLUMNS = V1_PROJECT_COLUMNS | {"repo_fingerprint_json"}
V3_PROJECT_COLUMNS = V2_PROJECT_COLUMNS | {
    "repo_binding_generation",
    "snapshot_binding_generation",
}
REGISTRY_EVENT_COLUMNS = {
    "event_id",
    "project_id",
    "project_name",
    "event_type",
    "message",
    "details_json",
    "created_at",
}
META_COLUMNS = {"key", "value"}
REQUIRED_META_KEYS = {"schema_version", "created_at", "tool_version"}
V4_TABLE_SQL = {
    "workspaces": WORKSPACES_TABLE_SQL,
    "lifecycle_tasks": LIFECYCLE_TASKS_TABLE_SQL,
    "lifecycle_operations": LIFECYCLE_OPERATIONS_TABLE_SQL,
    "snapshot_generations": SNAPSHOT_GENERATIONS_TABLE_SQL,
    "managed_pointers": MANAGED_POINTERS_TABLE_SQL,
}


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    tool_version: str,
    event_id: Callable[[], str],
    allow_create: bool = True,
) -> None:
    """Initialize a new registry or migrate a supported registry atomically."""

    conn.execute("PRAGMA foreign_keys = ON")
    tables = _table_names(conn)
    if not tables:
        if not allow_create:
            raise sqlite3.DatabaseError("Registry schema is invalid; no registry tables found")
        _run_schema_transaction(
            conn,
            lambda: _create_schema(conn, now=now, tool_version=tool_version, event_id=event_id),
        )
        return

    missing_tables = BASE_REQUIRED_TABLES - tables
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise sqlite3.DatabaseError(f"Registry schema is invalid; missing tables: {missing}")

    _require_columns(conn, "registry_events", REGISTRY_EVENT_COLUMNS)
    _require_columns(conn, "meta", META_COLUMNS)
    meta = _require_meta_values(conn)
    version = meta["schema_version"]

    if version == "1":
        _require_columns(conn, "projects", V1_PROJECT_COLUMNS)
    elif version == "2":
        _require_columns(conn, "projects", V2_PROJECT_COLUMNS)
    elif version in {V3_SCHEMA_VERSION, SCHEMA_VERSION}:
        _require_columns(conn, "projects", V3_PROJECT_COLUMNS)
    else:
        raise sqlite3.DatabaseError(f"Unsupported registry schema version: {version}")

    if version == SCHEMA_VERSION:
        _validate_v4_schema(conn)
        return

    def migrate() -> None:
        current = version
        if current in {"1", "2"}:
            _migrate_to_v3(conn, from_version=current, now=now, event_id=event_id)
            current = V3_SCHEMA_VERSION
        if current == V3_SCHEMA_VERSION:
            _migrate_to_v4(conn, now=now, event_id=event_id)
        _validate_v4_schema(conn)

    _run_schema_transaction(conn, migrate)


def _run_schema_transaction(conn: sqlite3.Connection, operation: Callable[[], None]) -> None:
    if conn.in_transaction:
        raise sqlite3.DatabaseError("Registry schema transaction must own the connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        operation()
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _create_schema(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    tool_version: str,
    event_id: Callable[[], str],
) -> None:
    conn.execute(PROJECTS_TABLE_SQL)
    conn.execute(REGISTRY_EVENTS_TABLE_SQL)
    conn.execute(META_TABLE_SQL)
    _create_v4_control_plane(conn)

    created_at = now()
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("schema_version", SCHEMA_VERSION),
            ("created_at", created_at),
            ("tool_version", tool_version),
        ],
    )
    conn.execute(
        """
        INSERT INTO registry_events (
            event_id, project_id, project_name, event_type, message, details_json, created_at
        )
        VALUES (?, NULL, NULL, ?, ?, NULL, ?)
        """,
        (event_id(), "schema_initialized", "Registry schema initialized.", created_at),
    )
    _validate_v4_schema(conn)


def _migrate_to_v3(
    conn: sqlite3.Connection,
    *,
    from_version: str,
    now: Callable[[], str],
    event_id: Callable[[], str],
) -> None:
    columns = _column_names(conn, "projects")
    if "repo_fingerprint_json" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_fingerprint_json TEXT NULL")
    if "repo_binding_generation" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN repo_binding_generation TEXT NULL")
    if "snapshot_binding_generation" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN snapshot_binding_generation TEXT NULL")
    projects = conn.execute(
        """SELECT project_id, project_name, created_at, updated_at,
                  last_status, last_indexed_at,
                  repo_binding_generation, snapshot_binding_generation
           FROM projects"""
    ).fetchall()
    for project in projects:
        repo_generation = project["repo_binding_generation"] or project["project_id"]
        snapshot_generation = project["snapshot_binding_generation"]
        if snapshot_generation is None:
            snapshot_generation = (
                _new_binding_generation(conn)
                if _legacy_history_requires_rebuild(conn, project)
                else repo_generation
            )
        conn.execute(
            """UPDATE projects
               SET repo_binding_generation = ?, snapshot_binding_generation = ?
               WHERE project_id = ?""",
            (repo_generation, snapshot_generation, project["project_id"]),
        )

    migrated_at = now()
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (V3_SCHEMA_VERSION,),
    )
    _log_migration(
        conn,
        event_id=event_id(),
        from_version=from_version,
        to_version=V3_SCHEMA_VERSION,
        migrated_at=migrated_at,
    )


def _migrate_to_v4(
    conn: sqlite3.Connection,
    *,
    now: Callable[[], str],
    event_id: Callable[[], str],
) -> None:
    _create_v4_tables_and_indexes(conn)
    projects = conn.execute(
        """SELECT project_id, repo_root, repo_root_norm, repo_fingerprint_json,
                  repo_binding_generation, created_at, updated_at
           FROM projects
           ORDER BY project_id"""
    ).fetchall()
    for project in projects:
        conn.execute(
            """INSERT INTO workspaces (
                   workspace_id, project_id, workspace_root, workspace_root_norm,
                   workspace_kind, repository_fingerprint_json,
                   workspace_binding_generation, workspace_state, row_version,
                   created_at, updated_at
               )
               VALUES (?, ?, ?, ?, 'PRIMARY', ?, ?, 'ACTIVE', 0, ?, ?)""",
            (
                _new_binding_generation(conn),
                project["project_id"],
                project["repo_root"],
                project["repo_root_norm"],
                project["repo_fingerprint_json"],
                project["repo_binding_generation"],
                project["created_at"],
                project["updated_at"],
            ),
        )
    _create_v4_triggers(conn)

    migrated_at = now()
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (SCHEMA_VERSION,),
    )
    _log_migration(
        conn,
        event_id=event_id(),
        from_version=V3_SCHEMA_VERSION,
        to_version=SCHEMA_VERSION,
        migrated_at=migrated_at,
    )


def _create_v4_control_plane(conn: sqlite3.Connection) -> None:
    _create_v4_tables_and_indexes(conn)
    _create_v4_triggers(conn)


def _create_v4_tables_and_indexes(conn: sqlite3.Connection) -> None:
    for table_sql in V4_TABLE_SQL.values():
        conn.execute(table_sql)
    for index_sql in INDEX_SQL.values():
        conn.execute(index_sql)


def _create_v4_triggers(conn: sqlite3.Connection) -> None:
    for trigger_sql in TRIGGER_SQL.values():
        conn.execute(trigger_sql)


def _log_migration(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    from_version: str,
    to_version: str,
    migrated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO registry_events (
            event_id, project_id, project_name, event_type, message, details_json, created_at
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            event_id,
            "schema_migrated",
            "Registry schema migrated.",
            json.dumps(
                {"from_version": from_version, "to_version": to_version},
                sort_keys=True,
            ),
            migrated_at,
        ),
    )


def _validate_v4_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("Registry schema is invalid; foreign keys are disabled")

    missing_tables = V4_REQUIRED_TABLES - _table_names(conn)
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise sqlite3.DatabaseError(f"Registry schema is invalid; missing tables: {missing}")

    _require_columns(conn, "projects", V3_PROJECT_COLUMNS)
    _require_columns(conn, "registry_events", REGISTRY_EVENT_COLUMNS)
    _require_columns(conn, "meta", META_COLUMNS)
    _require_primary_key(conn, "projects", ("project_id",))
    _require_primary_key(conn, "registry_events", ("event_id",))
    _require_primary_key(conn, "meta", ("key",))
    _require_unique_columns(conn, "projects", ("project_name_norm",))
    _require_unique_columns(conn, "projects", ("repo_root_norm",))
    for table_name, expected_sql in V4_TABLE_SQL.items():
        _require_schema_object_sql(conn, "table", table_name, expected_sql)
    for index_name, expected_sql in INDEX_SQL.items():
        _require_schema_object_sql(conn, "index", index_name, expected_sql)
    for trigger_name, expected_sql in TRIGGER_SQL.items():
        _require_schema_object_sql(conn, "trigger", trigger_name, expected_sql)

    foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_violations:
        raise sqlite3.DatabaseError("Registry schema is invalid; foreign-key ownership is violated")

    primary_counts = conn.execute(
        """SELECT p.project_id, COUNT(w.workspace_id) AS count
           FROM projects AS p
           LEFT JOIN workspaces AS w
             ON w.project_id = p.project_id AND w.workspace_kind = 'PRIMARY'
           GROUP BY p.project_id HAVING count != 1"""
    ).fetchall()
    project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    primary_count = conn.execute(
        "SELECT COUNT(*) FROM workspaces WHERE workspace_kind = 'PRIMARY'"
    ).fetchone()[0]
    if primary_counts or project_count != primary_count:
        raise sqlite3.DatabaseError(
            "Registry schema is invalid; each project must own exactly one PRIMARY workspace"
        )
    drift = conn.execute(
        """SELECT p.project_id
           FROM projects AS p
           JOIN workspaces AS w
             ON w.project_id = p.project_id AND w.workspace_kind = 'PRIMARY'
           WHERE p.repo_root IS NOT w.workspace_root
              OR p.repo_root_norm IS NOT w.workspace_root_norm
              OR p.repo_fingerprint_json IS NOT w.repository_fingerprint_json
              OR p.repo_binding_generation IS NOT w.workspace_binding_generation
           LIMIT 1"""
    ).fetchone()
    if drift is not None:
        raise sqlite3.DatabaseError(
            "Registry schema is invalid; project workspace compatibility projection drifted"
        )


def _require_schema_object_sql(
    conn: sqlite3.Connection,
    object_type: str,
    name: str,
    expected_sql: str,
) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, name),
    ).fetchone()
    if row is None or not isinstance(row["sql"], str):
        raise sqlite3.DatabaseError(f"Registry schema is invalid; missing {object_type}: {name}")
    if _normalize_schema_sql(row["sql"]) != _normalize_schema_sql(expected_sql):
        raise sqlite3.DatabaseError(
            f"Registry schema is invalid; {object_type} definition mismatch: {name}"
        )


def _normalize_schema_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split()).casefold()


def _legacy_history_requires_rebuild(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
) -> bool:
    """Fail closed when durable pre-v3 history cannot prove snapshot alignment."""

    if project["last_status"] == "RELINKED_REINDEX_REQUIRED":
        return True
    events = conn.execute(
        """SELECT event_type, details_json, created_at
           FROM registry_events
           WHERE project_id = ?
           ORDER BY created_at, event_id""",
        (project["project_id"],),
    ).fetchall()
    if not events or not any(event["event_type"] == "register" for event in events):
        return True

    relink_times: list[datetime] = []
    successful_index_times: list[datetime] = []
    for event in events:
        try:
            created_at = _parse_utc(event["created_at"])
        except TypeError, ValueError:
            return True
        if event["event_type"] == "relink":
            relink_times.append(created_at)
        elif event["event_type"] == "index_outcome":
            try:
                details = json.loads(event["details_json"])
            except TypeError, json.JSONDecodeError:
                return True
            if not isinstance(details, dict) or not isinstance(details.get("status"), str):
                return True
            if details["status"] == "INDEX_SUCCEEDED":
                successful_index_times.append(created_at)

    last_indexed_at = project["last_indexed_at"]
    if last_indexed_at is None:
        return True
    try:
        _parse_utc(last_indexed_at)
    except TypeError, ValueError:
        return True
    if not successful_index_times:
        return True

    if not relink_times:
        return False
    latest_relink = max(relink_times)
    return not any(indexed_at > latest_relink for indexed_at in successful_index_times)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("registry event timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("registry event timestamp must include UTC offset")
    return parsed


def _new_binding_generation(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT lower(hex(randomblob(16)))").fetchone()[0])


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows if not str(row["name"]).startswith("sqlite_")}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    missing = required - _column_names(conn, table)
    if missing:
        column_names = ", ".join(sorted(missing))
        raise sqlite3.DatabaseError(
            f"Registry schema is invalid; table {table} is missing columns: {column_names}"
        )


def _require_primary_key(
    conn: sqlite3.Connection,
    table: str,
    expected_columns: tuple[str, ...],
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    actual = tuple(
        str(row["name"])
        for row in sorted(rows, key=lambda row: int(row["pk"]))
        if int(row["pk"]) > 0
    )
    if actual != expected_columns:
        raise sqlite3.DatabaseError(f"Registry schema is invalid; primary key mismatch: {table}")


def _require_unique_columns(
    conn: sqlite3.Connection,
    table: str,
    expected_columns: tuple[str, ...],
) -> None:
    indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()
    for index in indexes:
        if int(index["unique"]) != 1 or int(index["partial"]) != 0:
            continue
        index_name = str(index["name"]).replace('"', '""')
        columns = tuple(
            str(row["name"])
            for row in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        )
        if columns == expected_columns:
            return
    joined = ", ".join(expected_columns)
    raise sqlite3.DatabaseError(
        f"Registry schema is invalid; missing UNIQUE constraint on {table}({joined})"
    )


def _require_meta_values(conn: sqlite3.Connection) -> dict[str, str]:
    values = {
        str(row["key"]): str(row["value"])
        for row in conn.execute("SELECT key, value FROM meta").fetchall()
    }
    missing = REQUIRED_META_KEYS - values.keys()
    if missing:
        key_names = ", ".join(sorted(missing))
        raise sqlite3.DatabaseError(f"Registry schema is invalid; missing meta keys: {key_names}")
    return values
