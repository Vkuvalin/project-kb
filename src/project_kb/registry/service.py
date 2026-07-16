"""Registry service for project registration and identity metadata."""

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project_kb.errors import (
    InvalidProjectNameError,
    ProjectNameAlreadyUsedError,
    ProjectNotRegisteredError,
    RegistryOperationError,
    RepoAlreadyRegisteredError,
    UnregisterRequiresYesError,
)
from project_kb.git_utils import resolve_git_root
from project_kb.registry.db import (
    immediate_registry_transaction,
    open_existing_registry,
    open_registry,
)
from project_kb.registry.models import WORKSPACE_STATES, ProjectRecord, WorkspaceRecord
from project_kb.resolver.repo_identity import (
    RepositoryFingerprint,
    build_repository_fingerprint,
)
from project_kb.storage.home import (
    create_project_storage,
    expected_project_storage_path,
    remove_project_storage,
    resolve_home,
    storage_path_matches_expected,
)

PROJECT_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")
PROJECT_WITH_PRIMARY_WORKSPACE_SQL = """
SELECT
    p.project_id,
    p.project_name,
    p.project_name_norm,
    p.storage_path,
    p.created_at,
    p.updated_at,
    p.last_status,
    p.last_indexed_at,
    p.last_git_commit,
    p.snapshot_binding_generation,
    w.workspace_id,
    w.workspace_kind,
    w.workspace_state,
    w.row_version AS workspace_row_version,
    w.workspace_root AS repo_root,
    w.workspace_root_norm AS repo_root_norm,
    w.repository_fingerprint_json AS repo_fingerprint_json,
    w.workspace_binding_generation AS repo_binding_generation,
    p.repo_root AS projected_repo_root,
    p.repo_root_norm AS projected_repo_root_norm,
    p.repo_fingerprint_json AS projected_repo_fingerprint_json,
    p.repo_binding_generation AS projected_repo_binding_generation
FROM projects AS p
JOIN workspaces AS w
  ON w.project_id = p.project_id AND w.workspace_kind = 'PRIMARY'
"""


@dataclass(frozen=True)
class RegistryResult:
    """Public result metadata returned by registry service operations."""

    code: str
    message: str
    project: ProjectRecord | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedStorageRemoval:
    original_path: Path
    prepared_path: Path | None
    storage_existed: bool


def _compare_and_swap_workspace_state(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    expected_row_version: int,
    expected_state: str,
    new_state: str,
    updated_at: str,
) -> int:
    """Mutate workspace state only inside a caller-owned registry transaction."""

    if not conn.in_transaction:
        raise RegistryOperationError("Workspace CAS requires an owned registry transaction.")
    if expected_state not in WORKSPACE_STATES or new_state not in WORKSPACE_STATES:
        raise RegistryOperationError(
            "Workspace state is invalid.",
            details={"expected_state": expected_state, "new_state": new_state},
        )
    cursor = conn.execute(
        """UPDATE workspaces
           SET workspace_state = ?, row_version = row_version + 1, updated_at = ?
           WHERE workspace_id = ? AND row_version = ? AND workspace_state = ?""",
        (new_state, updated_at, workspace_id, expected_row_version, expected_state),
    )
    if cursor.rowcount != 1:
        raise RegistryOperationError(
            "Workspace compare-and-swap precondition failed.",
            details={
                "workspace_id": workspace_id,
                "expected_row_version": expected_row_version,
                "expected_state": expected_state,
            },
        )
    return expected_row_version + 1


def _compare_and_swap_pointer(
    conn: sqlite3.Connection,
    pointer_id: str,
    *,
    expected_pointer_version: int,
    snapshot_id: str,
    updated_at: str,
) -> int:
    """Mutate one pointer only inside a caller-owned multi-row transaction."""

    if not conn.in_transaction:
        raise RegistryOperationError("Pointer CAS requires an owned registry transaction.")
    cursor = conn.execute(
        """UPDATE managed_pointers
           SET snapshot_id = ?, pointer_version = pointer_version + 1, updated_at = ?
           WHERE pointer_id = ? AND pointer_version = ?""",
        (snapshot_id, updated_at, pointer_id, expected_pointer_version),
    )
    if cursor.rowcount != 1:
        raise RegistryOperationError(
            "Managed pointer compare-and-swap precondition failed.",
            details={
                "pointer_id": pointer_id,
                "expected_pointer_version": expected_pointer_version,
            },
        )
    return expected_pointer_version + 1


class RegistryService:
    """Service boundary for Project KB registry operations."""

    def __init__(self, *, home: Path | None = None) -> None:
        self.home = home or resolve_home()

    def register(self, project_name: str, repo_path: str | Path) -> RegistryResult:
        project_name_norm = normalize_project_name(project_name)
        repo_root = resolve_git_root(repo_path)
        repo_root_norm = normalize_repo_root(repo_root)

        with open_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            existing_by_name = self._get_project_by_name_norm(conn, project_name_norm)
            existing_by_repo = self._get_project_by_repo_norm(conn, repo_root_norm)

            if existing_by_name and existing_by_repo:
                if existing_by_name.project_id != existing_by_repo.project_id:
                    raise ProjectNameAlreadyUsedError(
                        project_name,
                        existing_by_name.repo_root,
                    )
                return self._refresh_existing_registration(conn, existing_by_name)

            if existing_by_name:
                raise ProjectNameAlreadyUsedError(project_name, existing_by_name.repo_root)
            if existing_by_repo:
                raise RepoAlreadyRegisteredError(
                    repo_root=str(repo_root), project_name=existing_by_repo.project_name
                )

            return self._create_registration(
                conn, project_name, project_name_norm, repo_root, repo_root_norm
            )

    def list_projects(self) -> list[ProjectRecord]:
        with open_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            rows = conn.execute(
                f"{PROJECT_WITH_PRIMARY_WORKSPACE_SQL} ORDER BY p.project_name_norm"
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def find_project_by_name(self, project_name: str) -> ProjectRecord | None:
        """Look up a project without creating a missing registry."""

        project_name_norm = normalize_project_name(project_name)
        with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            if conn is None:
                return None
            return self._get_project_by_name_norm(conn, project_name_norm)

    def find_project_by_repo_root(self, repo_root: Path) -> ProjectRecord | None:
        """Look up a normalized Git root without creating a missing registry."""

        repo_root_norm = normalize_repo_root(repo_root)
        with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            if conn is None:
                return None
            return self._get_project_by_repo_norm(conn, repo_root_norm)

    def find_workspace_by_id(self, workspace_id: str) -> WorkspaceRecord | None:
        """Look up an authoritative workspace without creating a missing registry."""

        with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            if conn is None:
                return None
            return self._get_workspace_by_id(conn, workspace_id)

    def initialize_repo_fingerprint(
        self,
        project_id: str,
        fingerprint: RepositoryFingerprint,
    ) -> tuple[ProjectRecord, bool]:
        """Initialize nullable legacy fingerprint metadata and record one registry event."""

        with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            if conn is None:
                raise RegistryOperationError(
                    "Registry disappeared during fingerprint initialization."
                )
            project = self._require_project_by_id(conn, project_id)
            if project.repo_fingerprint_json is not None:
                return project, False

            expected_workspace_id = project.workspace_id
            expected_workspace_version = project.workspace_row_version
            updated_at = utc_now()
            with immediate_registry_transaction(conn):
                project = self._require_project_by_id(conn, project_id)
                if project.repo_fingerprint_json is not None:
                    return project, False
                if (
                    project.workspace_id != expected_workspace_id
                    or project.workspace_row_version != expected_workspace_version
                ):
                    raise RegistryOperationError(
                        "Workspace changed during fingerprint initialization.",
                        details={"workspace_id": expected_workspace_id},
                    )
                cursor = conn.execute(
                    """
                    UPDATE workspaces
                    SET repository_fingerprint_json = ?,
                        row_version = row_version + 1,
                        updated_at = ?
                    WHERE workspace_id = ?
                      AND row_version = ?
                      AND repository_fingerprint_json IS NULL
                    """,
                    (
                        fingerprint.to_json(),
                        updated_at,
                        expected_workspace_id,
                        expected_workspace_version,
                    ),
                )
                initialized = cursor.rowcount == 1
                if initialized:
                    self._log_event(
                        conn,
                        project_id=project.project_id,
                        project_name=project.project_name,
                        event_type="repo_fingerprint_initialized",
                        message="Repository fingerprint initialized.",
                        details={"fingerprint_strength": fingerprint.fingerprint_strength},
                    )

            return self._require_project_by_id(conn, project_id), initialized

    def record_index_outcome(
        self,
        project_id: str,
        *,
        status: str,
        indexed_at: str | None = None,
        git_commit: str | None = None,
        failure_code: str | None = None,
        binding_generation: str | None = None,
    ) -> ProjectRecord:
        """Record the latest index outcome without changing registry schema contracts."""

        with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            if conn is None:
                raise RegistryOperationError("Registry disappeared while recording index outcome.")
            updated_at = precise_utc_now()
            with immediate_registry_transaction(conn):
                project = self._require_project_by_id(conn, project_id)
                if status == "INDEX_SUCCEEDED" and (
                    binding_generation is None
                    or binding_generation != project.repo_binding_generation
                ):
                    raise RegistryOperationError(
                        "Repository binding changed before the index outcome could be recorded."
                    )
                snapshot_binding_generation = (
                    binding_generation if status == "INDEX_SUCCEEDED" else None
                )
                conn.execute(
                    """
                    UPDATE projects
                    SET last_status = ?,
                        last_indexed_at = COALESCE(?, last_indexed_at),
                        last_git_commit = COALESCE(?, last_git_commit),
                        snapshot_binding_generation = COALESCE(
                            ?, snapshot_binding_generation
                        ),
                        updated_at = ?
                    WHERE project_id = ?
                    """,
                    (
                        status,
                        indexed_at,
                        git_commit,
                        snapshot_binding_generation,
                        updated_at,
                        project_id,
                    ),
                )
                self._log_event(
                    conn,
                    project_id=project.project_id,
                    project_name=project.project_name,
                    event_type="index_outcome",
                    message="Structural index outcome recorded.",
                    details={"status": status, "failure_code": failure_code},
                )
            return self._require_project_by_id(conn, project_id)

    def relink(self, project_name: str, new_repo_path: str | Path) -> RegistryResult:
        project_name_norm = normalize_project_name(project_name)
        repo_root = resolve_git_root(new_repo_path)
        repo_root_norm = normalize_repo_root(repo_root)
        fingerprint = build_repository_fingerprint(repo_root)

        with open_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            updated_at = utc_now()
            fingerprint_json = fingerprint.to_json()
            binding_generation = new_id()
            last_status = "RELINKED_REINDEX_REQUIRED"
            with immediate_registry_transaction(conn):
                project = self._require_project_by_name_norm(conn, project_name, project_name_norm)
                if self._has_open_lineage(
                    conn, project.workspace_id
                ) or self._has_unresolved_operation(conn, project.workspace_id):
                    raise RegistryOperationError(
                        "Cannot relink a workspace while it owns open lifecycle work.",
                        details={"workspace_id": project.workspace_id},
                    )
                existing_by_repo = self._get_project_by_repo_norm(conn, repo_root_norm)
                if existing_by_repo and existing_by_repo.project_id != project.project_id:
                    raise RepoAlreadyRegisteredError(
                        repo_root=str(repo_root),
                        project_name=existing_by_repo.project_name,
                    )
                binding_changed = (
                    project.repo_root_norm != repo_root_norm
                    or project.repo_fingerprint_json != fingerprint_json
                )
                cursor = conn.execute(
                    """
                    UPDATE workspaces
                    SET workspace_root = ?,
                        workspace_root_norm = ?,
                        repository_fingerprint_json = ?,
                        workspace_binding_generation = ?,
                        workspace_state = 'ACTIVE',
                        row_version = row_version + 1,
                        updated_at = ?
                    WHERE workspace_id = ? AND row_version = ?
                    """,
                    (
                        str(repo_root),
                        repo_root_norm,
                        fingerprint_json,
                        binding_generation,
                        updated_at,
                        project.workspace_id,
                        project.workspace_row_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RegistryOperationError(
                        "Workspace compare-and-swap precondition failed during relink.",
                        details={"workspace_id": project.workspace_id},
                    )
                conn.execute(
                    "UPDATE projects SET last_status = ?, updated_at = ? WHERE project_id = ?",
                    (last_status, updated_at, project.project_id),
                )
                self._log_event(
                    conn,
                    project_id=project.project_id,
                    project_name=project.project_name,
                    event_type="relink",
                    message="Project repository root relinked.",
                    details={
                        "repo_root": str(repo_root),
                        "fingerprint_strength": fingerprint.fingerprint_strength,
                        "identity_changed": binding_changed,
                        "rebuild_required": True,
                    },
                )

            updated_project = self._require_project_by_id(conn, project.project_id)
        return RegistryResult(
            code="PROJECT_RELINKED",
            message="Project repository root relinked.",
            project=updated_project,
            data={"rebuild_required": True, "identity_changed": binding_changed},
        )

    def unregister(self, project_name: str, *, yes: bool) -> RegistryResult:
        project_name_norm = normalize_project_name(project_name)
        if not yes:
            raise UnregisterRequiresYesError(project_name)

        with (
            open_registry(home=self.home, now=utc_now, event_id=new_id) as conn,
            immediate_registry_transaction(conn),
        ):
            project = self._require_project_by_name_norm(conn, project_name, project_name_norm)
            self._require_legacy_unregister_preconditions(conn, project)
            if project.workspace_state != "ACTIVE":
                raise RegistryOperationError(
                    "Legacy unregister requires an ACTIVE workspace.",
                    details={
                        "project_id": project.project_id,
                        "workspace_id": project.workspace_id,
                        "workspace_state": project.workspace_state,
                    },
                )
            claimed_workspace_version = _compare_and_swap_workspace_state(
                conn,
                project.workspace_id,
                expected_row_version=project.workspace_row_version,
                expected_state="ACTIVE",
                new_state="UNAVAILABLE",
                updated_at=precise_utc_now(),
            )

        storage_path = Path(project.storage_path)
        prepared = _PreparedStorageRemoval(
            original_path=storage_path,
            prepared_path=None,
            storage_existed=storage_path.exists(),
        )
        try:
            prepared = _prepare_project_storage_removal(
                storage_path,
                project_id=project.project_id,
            )
            _require_prepared_storage_state(prepared)
            with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
                if conn is None:
                    raise RegistryOperationError("Registry disappeared during unregister.")
                with immediate_registry_transaction(conn):
                    current = self._require_project_by_id(conn, project.project_id)
                    self._require_unregister_claim(
                        current,
                        original=project,
                        claimed_workspace_version=claimed_workspace_version,
                    )
                    self._require_legacy_unregister_preconditions(conn, current)
                    cursor = conn.execute(
                        """DELETE FROM workspaces
                           WHERE workspace_id = ? AND workspace_kind = 'PRIMARY'
                             AND workspace_state = 'UNAVAILABLE' AND row_version = ?""",
                        (project.workspace_id, claimed_workspace_version),
                    )
                    if cursor.rowcount != 1:
                        raise RegistryOperationError(
                            "Primary workspace changed during unregister.",
                            details={
                                "workspace_id": project.workspace_id,
                                "expected_row_version": claimed_workspace_version,
                            },
                        )
                    self._log_event(
                        conn,
                        project_id=project.project_id,
                        project_name=project.project_name,
                        event_type="unregister",
                        message="Project unregistered.",
                        details={"storage_path": project.storage_path},
                    )
        except BaseException as exc:
            compensation = self._compensate_unregister(
                project,
                claimed_workspace_version=claimed_workspace_version,
                prepared=prepared,
            )
            if not compensation["storage_restored"] or not compensation["claim_released"]:
                raise RegistryOperationError(
                    "Unregister failed and the workspace remains fail-closed.",
                    details={
                        "project_id": project.project_id,
                        "workspace_id": project.workspace_id,
                        **compensation,
                    },
                ) from exc
            raise

        removed_paths = _removed_storage_paths(storage_path, storage_deleted=False)
        if prepared.prepared_path is not None:
            try:
                deletion = remove_project_storage(prepared.prepared_path, home=self.home)
            except RegistryOperationError as exc:
                raise RegistryOperationError(
                    "Project was unregistered, but prepared project storage could not be deleted.",
                    details={
                        "project_id": project.project_id,
                        "prepared_storage_path": str(prepared.prepared_path),
                        "registry_row_present": False,
                    },
                ) from exc
            removed_paths = _removed_storage_paths(
                storage_path,
                storage_deleted=bool(deletion["storage_deleted"]),
            )
            if removed_paths["storage_deleted"]:
                try:
                    self._record_storage_deleted_event(project)
                except BaseException as exc:
                    raise RegistryOperationError(
                        "Project was unregistered and storage was deleted, "
                        "but audit logging failed.",
                        details={
                            "project_id": project.project_id,
                            "storage_path": project.storage_path,
                            "registry_row_present": False,
                        },
                    ) from exc

        return RegistryResult(
            code="PROJECT_UNREGISTERED",
            message="Project unregistered.",
            project=project,
            data={"removed_paths": removed_paths},
        )

    def _require_legacy_unregister_preconditions(
        self,
        conn: sqlite3.Connection,
        project: ProjectRecord,
    ) -> None:
        if not storage_path_matches_expected(
            Path(project.storage_path),
            home=self.home,
            project_id=project.project_id,
        ):
            raise RegistryOperationError(
                "Refusing to unregister a project with an unexpected storage path.",
                details={
                    "project_id": project.project_id,
                    "storage_path": project.storage_path,
                    "expected_storage_path": str(
                        expected_project_storage_path(self.home, project.project_id)
                    ),
                },
            )
        if self._has_lifecycle_history(conn, project.project_id):
            raise RegistryOperationError(
                "Lifecycle-bearing projects cannot be hard-unregistered.",
                details={"project_id": project.project_id},
            )
        workspace_count = conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE project_id = ?",
            (project.project_id,),
        ).fetchone()[0]
        if workspace_count != 1:
            raise RegistryOperationError(
                "Projects with additional workspaces cannot be hard-unregistered.",
                details={
                    "project_id": project.project_id,
                    "workspace_count": workspace_count,
                },
            )

    def _require_unregister_claim(
        self,
        current: ProjectRecord,
        *,
        original: ProjectRecord,
        claimed_workspace_version: int,
    ) -> None:
        if (
            current.project_id != original.project_id
            or current.workspace_id != original.workspace_id
            or current.repo_binding_generation != original.repo_binding_generation
            or current.repo_root_norm != original.repo_root_norm
            or current.storage_path != original.storage_path
            or current.workspace_row_version != claimed_workspace_version
            or current.workspace_state != "UNAVAILABLE"
        ):
            raise RegistryOperationError(
                "Project or workspace changed during unregister.",
                details={
                    "project_id": original.project_id,
                    "workspace_id": original.workspace_id,
                    "expected_row_version": claimed_workspace_version,
                    "actual_row_version": current.workspace_row_version,
                    "actual_workspace_state": current.workspace_state,
                },
            )

    def _compensate_unregister(
        self,
        project: ProjectRecord,
        *,
        claimed_workspace_version: int,
        prepared: _PreparedStorageRemoval,
    ) -> dict[str, bool]:
        storage_restored = _restore_prepared_project_storage(prepared)
        claim_released = False
        if storage_restored:
            try:
                with open_existing_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
                    if conn is not None:
                        with immediate_registry_transaction(conn):
                            workspace = self._get_workspace_by_id(conn, project.workspace_id)
                            if (
                                workspace is not None
                                and workspace.project_id == project.project_id
                                and workspace.workspace_binding_generation
                                == project.repo_binding_generation
                                and workspace.row_version == claimed_workspace_version
                                and workspace.workspace_state == "UNAVAILABLE"
                            ):
                                _compare_and_swap_workspace_state(
                                    conn,
                                    project.workspace_id,
                                    expected_row_version=claimed_workspace_version,
                                    expected_state="UNAVAILABLE",
                                    new_state="ACTIVE",
                                    updated_at=precise_utc_now(),
                                )
                                claim_released = True
            except BaseException:
                claim_released = False
        return {
            "storage_restored": storage_restored,
            "claim_released": claim_released,
        }

    def _record_storage_deleted_event(self, project: ProjectRecord) -> None:
        with (
            open_registry(home=self.home, now=utc_now, event_id=new_id) as conn,
            immediate_registry_transaction(conn),
        ):
            self._log_event(
                conn,
                project_id=project.project_id,
                project_name=project.project_name,
                event_type="storage_deleted",
                message="Project storage directory deleted.",
                details={"storage_path": project.storage_path},
            )

    def _create_registration(
        self,
        conn: sqlite3.Connection,
        project_name: str,
        project_name_norm: str,
        repo_root: Path,
        repo_root_norm: str,
    ) -> RegistryResult:
        project_id = new_id()
        binding_generation = new_id()
        storage_path = expected_project_storage_path(self.home, project_id)
        if not storage_path_matches_expected(
            storage_path,
            home=self.home,
            project_id=project_id,
        ):
            raise RegistryOperationError(
                "Refusing to create project storage outside the Project KB home.",
                details={
                    "project_id": project_id,
                    "storage_path": str(storage_path),
                },
            )
        fingerprint = build_repository_fingerprint(repo_root)
        storage_paths = create_project_storage(storage_path)
        created_at = utc_now()

        with immediate_registry_transaction(conn):
            existing_by_name = self._get_project_by_name_norm(conn, project_name_norm)
            if existing_by_name is not None:
                raise ProjectNameAlreadyUsedError(project_name, existing_by_name.repo_root)
            existing_by_repo = self._get_project_by_repo_norm(conn, repo_root_norm)
            if existing_by_repo is not None:
                raise RepoAlreadyRegisteredError(
                    repo_root=str(repo_root), project_name=existing_by_repo.project_name
                )
            conn.execute(
                """
                INSERT INTO projects (
                    project_id,
                    project_name,
                    project_name_norm,
                    repo_root,
                    repo_root_norm,
                    storage_path,
                    created_at,
                    updated_at,
                    last_status,
                    last_indexed_at,
                    last_git_commit,
                    repo_fingerprint_json,
                    repo_binding_generation,
                    snapshot_binding_generation
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    project_id,
                    project_name,
                    project_name_norm,
                    str(repo_root),
                    repo_root_norm,
                    str(storage_path),
                    created_at,
                    created_at,
                    "REGISTERED",
                    fingerprint.to_json(),
                    binding_generation,
                    binding_generation,
                ),
            )
            self._log_event(
                conn,
                project_id=project_id,
                project_name=project_name,
                event_type="storage_created",
                message="Project storage directory created.",
                details=storage_paths,
            )
            self._log_event(
                conn,
                project_id=project_id,
                project_name=project_name,
                event_type="register",
                message="Project registered.",
                details={
                    "repo_root": str(repo_root),
                    "storage_path": str(storage_path),
                    "fingerprint_strength": fingerprint.fingerprint_strength,
                },
            )

        project = self._require_project_by_id(conn, project_id)
        return RegistryResult(
            code="PROJECT_REGISTERED",
            message="Project registered.",
            project=project,
        )

    def _refresh_existing_registration(
        self,
        conn: sqlite3.Connection,
        project: ProjectRecord,
    ) -> RegistryResult:
        if project.workspace_state != "ACTIVE":
            raise RegistryOperationError(
                "Project registration cannot refresh a non-ACTIVE workspace.",
                details={
                    "workspace_id": project.workspace_id,
                    "workspace_state": project.workspace_state,
                },
            )
        if not storage_path_matches_expected(
            Path(project.storage_path),
            home=self.home,
            project_id=project.project_id,
        ):
            raise RegistryOperationError(
                "Refusing to refresh project storage at an unexpected path.",
                details={
                    "project_id": project.project_id,
                    "storage_path": project.storage_path,
                    "expected_storage_path": str(
                        expected_project_storage_path(self.home, project.project_id)
                    ),
                },
            )

        create_project_storage(Path(project.storage_path))
        updated_at = utc_now()
        fingerprint = (
            build_repository_fingerprint(Path(project.repo_root))
            if project.repo_fingerprint_json is None
            else None
        )

        with immediate_registry_transaction(conn):
            current = self._require_project_by_id(conn, project.project_id)
            if (
                current.workspace_id != project.workspace_id
                or current.workspace_row_version != project.workspace_row_version
                or current.workspace_state != "ACTIVE"
            ):
                raise RegistryOperationError(
                    "Workspace changed while registration was being refreshed.",
                    details={"workspace_id": project.workspace_id},
                )
            if fingerprint is not None:
                cursor = conn.execute(
                    """UPDATE workspaces
                       SET repository_fingerprint_json = ?,
                           row_version = row_version + 1,
                           updated_at = ?
                       WHERE workspace_id = ?
                         AND row_version = ?
                         AND repository_fingerprint_json IS NULL""",
                    (
                        fingerprint.to_json(),
                        updated_at,
                        project.workspace_id,
                        project.workspace_row_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RegistryOperationError(
                        "Workspace compare-and-swap precondition failed during refresh.",
                        details={"workspace_id": project.workspace_id},
                    )
            conn.execute(
                """
                UPDATE projects
                SET updated_at = ?,
                    last_status = ?
                WHERE project_id = ?
                """,
                (
                    updated_at,
                    "REGISTERED",
                    project.project_id,
                ),
            )
            if fingerprint is not None:
                self._log_event(
                    conn,
                    project_id=project.project_id,
                    project_name=project.project_name,
                    event_type="repo_fingerprint_initialized",
                    message="Repository fingerprint initialized.",
                    details={"fingerprint_strength": fingerprint.fingerprint_strength},
                )
            self._log_event(
                conn,
                project_id=project.project_id,
                project_name=project.project_name,
                event_type="register_idempotent_refresh",
                message="Project registration refreshed.",
                details={"repo_root": project.repo_root, "storage_path": project.storage_path},
            )

        refreshed_project = self._require_project_by_id(conn, project.project_id)
        return RegistryResult(
            code="PROJECT_ALREADY_REGISTERED_REFRESHED",
            message="Project registration refreshed.",
            project=refreshed_project,
        )

    def _get_project_by_name_norm(
        self,
        conn: sqlite3.Connection,
        project_name_norm: str,
    ) -> ProjectRecord | None:
        row = conn.execute(
            f"{PROJECT_WITH_PRIMARY_WORKSPACE_SQL} WHERE p.project_name_norm = ?",
            (project_name_norm,),
        ).fetchone()
        if (
            row is None
            and conn.execute(
                "SELECT 1 FROM projects WHERE project_name_norm = ?",
                (project_name_norm,),
            ).fetchone()
        ):
            raise RegistryOperationError(
                "Project record has no valid authoritative PRIMARY workspace."
            )
        return self._project_from_row(row) if row else None

    def _get_project_by_repo_norm(
        self,
        conn: sqlite3.Connection,
        repo_root_norm: str,
    ) -> ProjectRecord | None:
        row = conn.execute(
            f"{PROJECT_WITH_PRIMARY_WORKSPACE_SQL} WHERE w.workspace_root_norm = ?",
            (repo_root_norm,),
        ).fetchone()
        return self._project_from_row(row) if row else None

    def _require_project_by_id(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> ProjectRecord:
        row = conn.execute(
            f"{PROJECT_WITH_PRIMARY_WORKSPACE_SQL} WHERE p.project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise RegistryOperationError(
                "Registry project record disappeared during operation.",
                details={"project_id": project_id},
            )
        return self._project_from_row(row)

    def _get_workspace_by_id(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
    ) -> WorkspaceRecord | None:
        row = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return WorkspaceRecord.from_row(row) if row else None

    def _project_from_row(self, row: sqlite3.Row) -> ProjectRecord:
        projection_pairs = (
            ("repo_root", "projected_repo_root"),
            ("repo_root_norm", "projected_repo_root_norm"),
            ("repo_fingerprint_json", "projected_repo_fingerprint_json"),
            ("repo_binding_generation", "projected_repo_binding_generation"),
        )
        drifted = [
            authoritative
            for authoritative, projected in projection_pairs
            if row[authoritative] != row[projected]
        ]
        if drifted:
            raise RegistryOperationError(
                "Project workspace compatibility projection drifted.",
                details={
                    "project_id": row["project_id"],
                    "fields": sorted(drifted),
                },
            )
        return ProjectRecord.from_row(row)

    def _has_open_lineage(self, conn: sqlite3.Connection, workspace_id: str) -> bool:
        return (
            conn.execute(
                """SELECT 1 FROM lifecycle_tasks
                   WHERE workspace_id = ? AND task_state IN ('DRAFT', 'ACTIVE', 'BLOCKED')
                   LIMIT 1""",
                (workspace_id,),
            ).fetchone()
            is not None
        )

    def _has_unresolved_operation(self, conn: sqlite3.Connection, workspace_id: str) -> bool:
        return (
            conn.execute(
                """SELECT 1 FROM lifecycle_operations
                   WHERE workspace_id = ?
                     AND operation_phase IN (
                         'RESERVED', 'BUILDING', 'FILE_PUBLISHED', 'RECOVERY_REQUIRED'
                     )
                   LIMIT 1""",
                (workspace_id,),
            ).fetchone()
            is not None
        )

    def _has_lifecycle_history(self, conn: sqlite3.Connection, project_id: str) -> bool:
        for table in (
            "lifecycle_tasks",
            "lifecycle_operations",
            "snapshot_generations",
            "managed_pointers",
        ):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1",
                (project_id,),
            ).fetchone():
                return True
        return False

    def _require_project_by_name_norm(
        self,
        conn: sqlite3.Connection,
        project_name: str,
        project_name_norm: str,
    ) -> ProjectRecord:
        project = self._get_project_by_name_norm(conn, project_name_norm)
        if project is None:
            raise ProjectNotRegisteredError(project_name)
        return project

    def _log_event(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str | None,
        project_name: str | None,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO registry_events (
                event_id,
                project_id,
                project_name,
                event_type,
                message,
                details_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                project_id,
                project_name,
                event_type,
                message,
                json.dumps(details, sort_keys=True) if details is not None else None,
                utc_now(),
            ),
        )


def _prepare_project_storage_removal(
    storage_path: Path,
    *,
    project_id: str,
) -> _PreparedStorageRemoval:
    if not storage_path.exists():
        return _PreparedStorageRemoval(
            original_path=storage_path,
            prepared_path=None,
            storage_existed=False,
        )
    try:
        redirected = storage_path.is_symlink() or storage_path.is_junction()
        is_directory = storage_path.is_dir()
    except OSError as exc:
        raise RegistryOperationError(
            "Project storage could not be inspected before unregister.",
            details={"storage_path": str(storage_path), "os_error": str(exc)},
        ) from exc
    if redirected or not is_directory:
        raise RegistryOperationError(
            "Refusing to prepare unsafe project storage for unregister.",
            details={"storage_path": str(storage_path)},
        )

    prepared_path = storage_path.with_name(f".{project_id}.unregister-{new_id()}")
    if prepared_path.exists():
        raise RegistryOperationError(
            "Project unregister preparation path already exists.",
            details={"prepared_storage_path": str(prepared_path)},
        )
    try:
        storage_path.rename(prepared_path)
    except OSError as exc:
        raise RegistryOperationError(
            "Project storage could not be prepared for unregister.",
            details={"storage_path": str(storage_path), "os_error": str(exc)},
        ) from exc
    return _PreparedStorageRemoval(
        original_path=storage_path,
        prepared_path=prepared_path,
        storage_existed=True,
    )


def _require_prepared_storage_state(prepared: _PreparedStorageRemoval) -> None:
    if prepared.storage_existed:
        valid = (
            prepared.prepared_path is not None
            and prepared.prepared_path.is_dir()
            and not prepared.original_path.exists()
        )
    else:
        valid = prepared.prepared_path is None and not prepared.original_path.exists()
    if not valid:
        raise RegistryOperationError(
            "Project storage changed during unregister preparation.",
            details={
                "storage_path": str(prepared.original_path),
                "prepared_storage_path": (
                    str(prepared.prepared_path) if prepared.prepared_path is not None else None
                ),
            },
        )


def _restore_prepared_project_storage(prepared: _PreparedStorageRemoval) -> bool:
    if not prepared.storage_existed:
        return False
    if prepared.prepared_path is None:
        return prepared.original_path.is_dir()
    if prepared.original_path.exists() or not prepared.prepared_path.is_dir():
        return False
    try:
        prepared.prepared_path.rename(prepared.original_path)
    except OSError:
        return False
    return prepared.original_path.is_dir() and not prepared.prepared_path.exists()


def _removed_storage_paths(storage_path: Path, *, storage_deleted: bool) -> dict[str, object]:
    return {
        "storage_path": str(storage_path),
        "exports_path": str(storage_path / "exports"),
        "runs_path": str(storage_path / "runs"),
        "storage_deleted": storage_deleted,
    }


def normalize_project_name(project_name: str) -> str:
    if not PROJECT_NAME_PATTERN.fullmatch(project_name):
        raise InvalidProjectNameError(project_name)
    return project_name.casefold()


def normalize_repo_root(repo_root: Path) -> str:
    return os.path.normcase(os.path.abspath(repo_root))


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def precise_utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex
