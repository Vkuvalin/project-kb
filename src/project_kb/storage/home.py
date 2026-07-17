"""Project KB storage home resolution and directory helpers."""

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from project_kb.errors import RegistryOperationError

GENERATION_STORAGE_LAYOUT_VERSION = 1
_MANAGED_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class ManagedGenerationPaths:
    """Canonical managed paths for one reserved lifecycle generation."""

    home: Path
    generation_root: Path
    shard_path: Path
    relative_final_path: str
    final_path: Path
    relative_temp_path: str
    temp_path: Path
    layout_version: int = GENERATION_STORAGE_LAYOUT_VERSION


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


def managed_generation_paths(
    home: Path,
    *,
    snapshot_id: str,
    operation_id: str,
) -> ManagedGenerationPaths:
    """Derive the sole v1 final/temp paths from validated immutable identities."""

    _require_managed_id(snapshot_id, field="snapshot_id")
    _require_managed_id(operation_id, field="operation_id")
    managed_home = _canonical_unredirected_path(home, field="Project KB home")
    shard = snapshot_id[:2]
    relative_final = PurePosixPath(
        "generations",
        shard,
        f"{snapshot_id}.sqlite",
    ).as_posix()
    relative_temp = PurePosixPath(
        "generations",
        shard,
        f".{snapshot_id}.{operation_id}.tmp.sqlite",
    ).as_posix()
    generation_root = managed_home / "generations"
    shard_path = generation_root / shard
    return ManagedGenerationPaths(
        home=managed_home,
        generation_root=generation_root,
        shard_path=shard_path,
        relative_final_path=relative_final,
        final_path=shard_path / f"{snapshot_id}.sqlite",
        relative_temp_path=relative_temp,
        temp_path=shard_path / f".{snapshot_id}.{operation_id}.tmp.sqlite",
    )


def prepare_managed_generation_storage(paths: ManagedGenerationPaths) -> None:
    """Create only the exact non-redirected managed directories for one generation."""

    expected = managed_generation_paths(
        paths.home,
        snapshot_id=paths.final_path.stem,
        operation_id=_operation_id_from_temp(paths.temp_path),
    )
    if paths != expected:
        raise RegistryOperationError(
            "Managed generation paths do not match the canonical layout.",
            details={"final_path": str(paths.final_path), "temp_path": str(paths.temp_path)},
        )
    _ensure_managed_directory(paths.home, parents=True)
    _ensure_managed_directory(paths.generation_root)
    _ensure_managed_directory(paths.shard_path)
    _require_contained(paths.final_path, paths.generation_root)
    _require_contained(paths.temp_path, paths.generation_root)


def resolve_managed_generation_file(
    home: Path,
    *,
    snapshot_id: str,
    relative_storage_path: str,
) -> Path:
    """Resolve one registry path only when it equals the canonical snapshot-derived path."""

    paths = managed_generation_paths(
        home,
        snapshot_id=snapshot_id,
        operation_id="0" * 32,
    )
    if relative_storage_path != paths.relative_final_path:
        raise RegistryOperationError(
            "Generation registry path is not the canonical managed relative path.",
            details={
                "snapshot_id": snapshot_id,
                "relative_storage_path": relative_storage_path,
                "expected_relative_storage_path": paths.relative_final_path,
            },
        )
    _require_existing_managed_directory(paths.home)
    _require_existing_managed_directory(paths.generation_root)
    _require_existing_managed_directory(paths.shard_path)
    _require_contained(paths.final_path, paths.generation_root)
    return paths.final_path


def require_managed_regular_file(path: Path, *, generation_root: Path) -> None:
    """Require an exact managed leaf to be a regular non-reparse file."""

    _require_contained(path, generation_root)
    try:
        path_stat = path.lstat()
        redirected = path.is_symlink() or path.is_junction()
    except OSError as exc:
        raise RegistryOperationError(
            "Managed generation file could not be inspected safely.",
            details={"generation_path": str(path), "os_error": str(exc)},
        ) from exc
    if redirected or not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
        raise RegistryOperationError(
            "Managed generation path must be a singly-linked regular non-redirected file.",
            details={
                "generation_path": str(path),
                "link_count": path_stat.st_nlink,
            },
        )
    resolved = path.resolve(strict=True)
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
        raise RegistryOperationError(
            "Managed generation path traverses redirected storage.",
            details={"generation_path": str(path), "resolved_path": str(resolved)},
        )


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


def _require_managed_id(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _MANAGED_ID_PATTERN.fullmatch(value) is None:
        raise RegistryOperationError(
            "Managed generation identity is invalid.",
            details={"field": field},
        )


def _canonical_unredirected_path(path: Path, *, field: str) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    try:
        resolved = absolute.resolve(strict=False)
    except OSError as exc:
        raise RegistryOperationError(
            f"{field} could not be resolved safely.",
            details={"path": str(absolute), "os_error": str(exc)},
        ) from exc
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(absolute)):
        raise RegistryOperationError(
            f"{field} traverses redirected storage.",
            details={"path": str(absolute), "resolved_path": str(resolved)},
        )
    return absolute


def _ensure_managed_directory(path: Path, *, parents: bool = False) -> None:
    if not os.path.lexists(path):
        try:
            path.mkdir(parents=parents, exist_ok=False)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RegistryOperationError(
                "Managed generation directory could not be created.",
                details={"generation_directory": str(path), "os_error": str(exc)},
            ) from exc
    _require_existing_managed_directory(path)


def _require_existing_managed_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
        redirected = path.is_symlink() or path.is_junction()
    except OSError as exc:
        raise RegistryOperationError(
            "Managed generation directory could not be inspected safely.",
            details={"generation_directory": str(path), "os_error": str(exc)},
        ) from exc
    if redirected or not stat.S_ISDIR(path_stat.st_mode):
        raise RegistryOperationError(
            "Managed generation directory must be non-redirected.",
            details={"generation_directory": str(path)},
        )
    resolved = path.resolve(strict=True)
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(path)):
        raise RegistryOperationError(
            "Managed generation directory traverses redirected storage.",
            details={"generation_directory": str(path), "resolved_path": str(resolved)},
        )


def _require_contained(path: Path, root: Path) -> None:
    candidate = os.path.normcase(os.path.abspath(path))
    allowed = os.path.normcase(os.path.abspath(root))
    try:
        contained = os.path.commonpath((candidate, allowed)) == allowed
    except ValueError:
        contained = False
    if not contained:
        raise RegistryOperationError(
            "Managed generation path escapes the generation root.",
            details={"generation_path": str(path), "generation_root": str(root)},
        )


def _operation_id_from_temp(temp_path: Path) -> str:
    parts = temp_path.name.split(".")
    if len(parts) != 5 or parts[0] or parts[3:] != ["tmp", "sqlite"]:
        raise RegistryOperationError(
            "Managed generation temp path is invalid.",
            details={"temp_path": str(temp_path)},
        )
    operation_id = parts[2]
    _require_managed_id(operation_id, field="operation_id")
    return operation_id


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
