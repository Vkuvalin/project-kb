"""Registry data models."""

from dataclasses import dataclass
from sqlite3 import Row


@dataclass(frozen=True)
class ProjectRecord:
    """A registered Project KB project."""

    project_id: str
    project_name: str
    project_name_norm: str
    repo_root: str
    repo_root_norm: str
    storage_path: str
    created_at: str
    updated_at: str
    last_status: str | None
    last_indexed_at: str | None
    last_git_commit: str | None

    @classmethod
    def from_row(cls, row: Row) -> ProjectRecord:
        return cls(
            project_id=row["project_id"],
            project_name=row["project_name"],
            project_name_norm=row["project_name_norm"],
            repo_root=row["repo_root"],
            repo_root_norm=row["repo_root_norm"],
            storage_path=row["storage_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_status=row["last_status"],
            last_indexed_at=row["last_indexed_at"],
            last_git_commit=row["last_git_commit"],
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "repo_root": self.repo_root,
            "storage_path": self.storage_path,
            "last_status": self.last_status,
            "last_indexed_at": self.last_indexed_at,
            "updated_at": self.updated_at,
        }
