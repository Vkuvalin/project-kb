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
from project_kb.registry.db import open_existing_registry, open_registry
from project_kb.registry.models import ProjectRecord
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


@dataclass(frozen=True)
class RegistryResult:
    """Public result metadata returned by registry service operations."""

    code: str
    message: str
    project: ProjectRecord | None = None
    data: dict[str, Any] = field(default_factory=dict)


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
                """
                SELECT *
                FROM projects
                ORDER BY project_name_norm
                """
            ).fetchall()
        return [ProjectRecord.from_row(row) for row in rows]

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

            updated_at = utc_now()
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE projects
                    SET repo_fingerprint_json = ?,
                        updated_at = ?
                    WHERE project_id = ?
                      AND repo_fingerprint_json IS NULL
                    """,
                    (fingerprint.to_json(), updated_at, project_id),
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
            project = self._require_project_by_id(conn, project_id)
            if status == "INDEX_SUCCEEDED" and (
                binding_generation is None or binding_generation != project.repo_binding_generation
            ):
                raise RegistryOperationError(
                    "Repository binding changed before the index outcome could be recorded."
                )
            snapshot_binding_generation = (
                binding_generation if status == "INDEX_SUCCEEDED" else None
            )
            updated_at = precise_utc_now()
            with conn:
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
            project = self._require_project_by_name_norm(conn, project_name, project_name_norm)
            existing_by_repo = self._get_project_by_repo_norm(conn, repo_root_norm)
            if existing_by_repo and existing_by_repo.project_id != project.project_id:
                raise RepoAlreadyRegisteredError(
                    repo_root=str(repo_root),
                    project_name=existing_by_repo.project_name,
                )

            updated_at = utc_now()
            fingerprint_json = fingerprint.to_json()
            binding_changed = (
                project.repo_root_norm != repo_root_norm
                or project.repo_fingerprint_json != fingerprint_json
            )
            binding_generation = new_id()
            last_status = "RELINKED_REINDEX_REQUIRED"
            with conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET repo_root = ?,
                        repo_root_norm = ?,
                        repo_fingerprint_json = ?,
                        repo_binding_generation = ?,
                        last_status = ?,
                        updated_at = ?
                    WHERE project_id = ?
                    """,
                    (
                        str(repo_root),
                        repo_root_norm,
                        fingerprint_json,
                        binding_generation,
                        last_status,
                        updated_at,
                        project.project_id,
                    ),
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

        with open_registry(home=self.home, now=utc_now, event_id=new_id) as conn:
            project = self._require_project_by_name_norm(conn, project_name, project_name_norm)
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
            with conn:
                removed_paths = remove_project_storage(Path(project.storage_path), home=self.home)
                conn.execute("DELETE FROM projects WHERE project_id = ?", (project.project_id,))
                if removed_paths["storage_deleted"]:
                    self._log_event(
                        conn,
                        project_id=project.project_id,
                        project_name=project.project_name,
                        event_type="storage_deleted",
                        message="Project storage directory deleted.",
                        details={"storage_path": project.storage_path},
                    )
                self._log_event(
                    conn,
                    project_id=project.project_id,
                    project_name=project.project_name,
                    event_type="unregister",
                    message="Project unregistered.",
                    details={"storage_path": project.storage_path},
                )

        return RegistryResult(
            code="PROJECT_UNREGISTERED",
            message="Project unregistered.",
            project=project,
            data={"removed_paths": removed_paths},
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

        with conn:
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

        with conn:
            conn.execute(
                """
                UPDATE projects
                SET updated_at = ?,
                    last_status = ?,
                    repo_fingerprint_json = COALESCE(repo_fingerprint_json, ?)
                WHERE project_id = ?
                """,
                (
                    updated_at,
                    "REGISTERED",
                    fingerprint.to_json() if fingerprint else None,
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
            "SELECT * FROM projects WHERE project_name_norm = ?",
            (project_name_norm,),
        ).fetchone()
        return ProjectRecord.from_row(row) if row else None

    def _get_project_by_repo_norm(
        self,
        conn: sqlite3.Connection,
        repo_root_norm: str,
    ) -> ProjectRecord | None:
        row = conn.execute(
            "SELECT * FROM projects WHERE repo_root_norm = ?",
            (repo_root_norm,),
        ).fetchone()
        return ProjectRecord.from_row(row) if row else None

    def _require_project_by_id(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> ProjectRecord:
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise RegistryOperationError(
                "Registry project record disappeared during operation.",
                details={"project_id": project_id},
            )
        return ProjectRecord.from_row(row)

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
