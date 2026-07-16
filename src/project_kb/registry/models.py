"""Registry data models."""

import re
from dataclasses import dataclass
from sqlite3 import Row

from project_kb.errors import RegistryOperationError

PROJECT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
PROJECT_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")
WORKSPACE_KINDS = frozenset({"PRIMARY", "LINKED"})
WORKSPACE_STATES = frozenset({"ACTIVE", "UNAVAILABLE", "RELINK_REQUIRED", "RETIRED"})


@dataclass(frozen=True)
class WorkspaceRecord:
    """Authoritative physical checkout/worktree identity."""

    workspace_id: str
    project_id: str
    workspace_root: str
    workspace_root_norm: str
    workspace_kind: str
    repository_fingerprint_json: str | None
    workspace_binding_generation: str
    workspace_state: str
    row_version: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> WorkspaceRecord:
        workspace_id = _identifier(row["workspace_id"], field="workspace_id")
        project_id = _identifier(row["project_id"], field="project_id")
        workspace_kind = row["workspace_kind"]
        workspace_state = row["workspace_state"]
        row_version = row["row_version"]
        if workspace_kind not in WORKSPACE_KINDS:
            raise RegistryOperationError("Registry workspace record contains an invalid kind.")
        if workspace_state not in WORKSPACE_STATES:
            raise RegistryOperationError("Registry workspace record contains an invalid state.")
        if not isinstance(row_version, int) or isinstance(row_version, bool) or row_version < 0:
            raise RegistryOperationError(
                "Registry workspace record contains an invalid row version."
            )
        return cls(
            workspace_id=workspace_id,
            project_id=project_id,
            workspace_root=row["workspace_root"],
            workspace_root_norm=row["workspace_root_norm"],
            workspace_kind=workspace_kind,
            repository_fingerprint_json=row["repository_fingerprint_json"],
            workspace_binding_generation=_binding_generation(
                row["workspace_binding_generation"],
                field="workspace_binding_generation",
            ),
            workspace_state=workspace_state,
            row_version=row_version,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class ProjectRecord:
    """A registered Project KB project."""

    project_id: str
    project_name: str
    project_name_norm: str
    workspace_id: str
    workspace_kind: str
    workspace_state: str
    workspace_row_version: int
    repo_root: str
    repo_root_norm: str
    storage_path: str
    created_at: str
    updated_at: str
    last_status: str | None
    last_indexed_at: str | None
    last_git_commit: str | None
    repo_fingerprint_json: str | None
    repo_binding_generation: str
    snapshot_binding_generation: str

    @classmethod
    def from_row(cls, row: Row) -> ProjectRecord:
        project_id = row["project_id"]
        project_name = row["project_name"]
        project_name_norm = row["project_name_norm"]
        _identifier(project_id, field="project_id")
        if (
            not isinstance(project_name, str)
            or not PROJECT_NAME_PATTERN.fullmatch(project_name)
            or project_name_norm != project_name.casefold()
        ):
            raise RegistryOperationError(
                "Registry project record contains an invalid project name."
            )

        return cls(
            project_id=project_id,
            project_name=project_name,
            project_name_norm=project_name_norm,
            workspace_id=_identifier(row["workspace_id"], field="workspace_id"),
            workspace_kind=_workspace_kind(row["workspace_kind"]),
            workspace_state=_workspace_state(row["workspace_state"]),
            workspace_row_version=_row_version(row["workspace_row_version"]),
            repo_root=row["repo_root"],
            repo_root_norm=row["repo_root_norm"],
            storage_path=row["storage_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_status=row["last_status"],
            last_indexed_at=row["last_indexed_at"],
            last_git_commit=row["last_git_commit"],
            repo_fingerprint_json=row["repo_fingerprint_json"],
            repo_binding_generation=_binding_generation(
                row["repo_binding_generation"],
                field="repo_binding_generation",
            ),
            snapshot_binding_generation=_binding_generation(
                row["snapshot_binding_generation"],
                field="snapshot_binding_generation",
            ),
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "repo_root": self.repo_root,
            "storage_path": self.storage_path,
            "created_at": self.created_at,
            "last_status": self.last_status,
            "last_indexed_at": self.last_indexed_at,
            "last_git_commit": self.last_git_commit,
            "updated_at": self.updated_at,
        }


def _binding_generation(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not PROJECT_ID_PATTERN.fullmatch(value):
        raise RegistryOperationError(f"Registry project record contains an invalid {field}.")
    return value


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not PROJECT_ID_PATTERN.fullmatch(value):
        raise RegistryOperationError(f"Registry record contains an invalid {field}.")
    return value


def _workspace_kind(value: object) -> str:
    if not isinstance(value, str) or value not in WORKSPACE_KINDS:
        raise RegistryOperationError("Registry project record contains an invalid workspace kind.")
    return value


def _workspace_state(value: object) -> str:
    if not isinstance(value, str) or value not in WORKSPACE_STATES:
        raise RegistryOperationError("Registry project record contains an invalid workspace state.")
    return value


def _row_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RegistryOperationError(
            "Registry project record contains an invalid workspace row version."
        )
    return value
