"""Project KB storage home resolution and directory helpers."""

import os
import shutil
from pathlib import Path

from project_kb.errors import RegistryOperationError


def resolve_home() -> Path:
    """Resolve the Project KB home using the current local-storage policy."""

    configured_home = os.environ.get("PROJECT_KB_HOME")
    if configured_home:
        return Path(configured_home).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local_app_data:
        return (Path(local_app_data).expanduser() / "project-kb").resolve()

    return (Path.home() / ".project-kb").resolve()


def expected_project_storage_path(home: Path, project_id: str) -> Path:
    """Return the one allowed storage directory for a registered project."""

    return Path(os.path.abspath(home / "projects" / project_id))


def storage_path_matches_expected(storage_path: Path, *, home: Path, project_id: str) -> bool:
    """Check exact storage ownership without creating or repairing any path."""

    resolved_home = home.expanduser().resolve(strict=False)
    projects_path = Path(os.path.abspath(resolved_home / "projects"))
    resolved_projects = projects_path.resolve(strict=False)
    if resolved_projects != projects_path or not resolved_projects.is_relative_to(resolved_home):
        return False

    expected = expected_project_storage_path(resolved_home, project_id)
    actual = Path(os.path.abspath(storage_path.expanduser()))
    if actual != expected:
        return False

    resolved_actual = actual.resolve(strict=False)
    return resolved_actual == expected and resolved_actual.is_relative_to(resolved_projects)


def create_project_storage(storage_path: Path) -> dict[str, str]:
    """Create the per-project storage directory layout."""

    try:
        _reject_redirected_storage_path(storage_path)
        storage_path.mkdir(parents=True, exist_ok=True)
        exports_path = storage_path / "exports"
        runs_path = storage_path / "runs"
        _reject_redirected_storage_path(exports_path)
        _reject_redirected_storage_path(runs_path)
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


def _reject_redirected_storage_path(path: Path) -> None:
    try:
        redirected = path.is_symlink() or path.is_junction()
    except OSError as exc:
        raise RegistryOperationError(
            "Project storage path could not be inspected safely.",
            details={"storage_path": str(path), "os_error": str(exc)},
        ) from exc
    if redirected:
        raise RegistryOperationError(
            "Refusing to create or refresh redirected Project KB storage.",
            details={"storage_path": str(path)},
        )


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
