"""Project KB storage home resolution and directory helpers."""

import os
import shutil
from pathlib import Path

from project_kb.errors import RegistryOperationError


def resolve_home() -> Path:
    """Resolve the Project KB home using the Stage 2 policy."""

    configured_home = os.environ.get("PROJECT_KB_HOME")
    if configured_home:
        return Path(configured_home).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local_app_data:
        return (Path(local_app_data).expanduser() / "project-kb").resolve()

    return (Path.home() / ".project-kb").resolve()


def create_project_storage(storage_path: Path) -> dict[str, str]:
    """Create the Stage 2 per-project storage directory layout."""

    try:
        storage_path.mkdir(parents=True, exist_ok=True)
        exports_path = storage_path / "exports"
        runs_path = storage_path / "runs"
        exports_path.mkdir(exist_ok=True)
        runs_path.mkdir(exist_ok=True)
    except OSError as exc:
        raise RegistryOperationError(
            "Project storage directory could not be created.",
            details={"storage_path": str(storage_path), "os_error": str(exc)},
        ) from exc

    return {
        "storage_path": str(storage_path),
        "exports_path": str(exports_path),
        "runs_path": str(runs_path),
    }


def remove_project_storage(storage_path: Path, *, home: Path) -> dict[str, object]:
    """Remove a project storage directory after checking it is inside the KB home."""

    resolved_home_projects = (home / "projects").resolve()
    resolved_storage_path = storage_path.resolve()
    if not resolved_storage_path.is_relative_to(resolved_home_projects):
        raise RegistryOperationError(
            "Refusing to delete project storage outside the Project KB projects directory.",
            details={
                "storage_path": str(resolved_storage_path),
                "allowed_root": str(resolved_home_projects),
            },
        )

    removed_paths: dict[str, object] = {
        "storage_path": str(resolved_storage_path),
        "exports_path": str(resolved_storage_path / "exports"),
        "runs_path": str(resolved_storage_path / "runs"),
        "storage_deleted": False,
    }
    if resolved_storage_path.exists():
        try:
            shutil.rmtree(resolved_storage_path)
        except OSError as exc:
            raise RegistryOperationError(
                "Project storage directory could not be deleted.",
                details={"storage_path": str(resolved_storage_path), "os_error": str(exc)},
            ) from exc
        removed_paths["storage_deleted"] = True
    return removed_paths
