"""Pure Project KB storage-layout diagnostics."""

from pathlib import Path

from project_kb.registry.models import ProjectRecord
from project_kb.resolver.models import StorageCheck
from project_kb.resolver.state import ProjectState
from project_kb.storage.home import (
    expected_project_storage_path,
    storage_path_matches_expected,
)


def check_project_storage(
    project: ProjectRecord,
    *,
    home: Path,
) -> tuple[StorageCheck, ProjectState | None]:
    """Inspect expected directories without creating, repairing, or deleting them."""

    expected = expected_project_storage_path(home, project.project_id)
    actual = Path(project.storage_path).expanduser().absolute()
    if not storage_path_matches_expected(
        actual,
        home=home,
        project_id=project.project_id,
    ):
        return (
            StorageCheck(
                status="mismatch",
                expected_storage_path=str(expected),
                actual_storage_path=str(actual),
                storage_exists=None,
                is_directory=None,
                exports_exists=None,
                runs_exists=None,
                missing_paths=(),
                reason="storage_path_mismatch",
            ),
            ProjectState.STORAGE_MISMATCH,
        )

    storage_exists = actual.exists()
    is_directory = actual.is_dir() if storage_exists else False
    exports_path = actual / "exports"
    runs_path = actual / "runs"
    exports_exists = _is_real_directory(exports_path) if is_directory else False
    runs_exists = _is_real_directory(runs_path) if is_directory else False

    missing_paths: list[str] = []
    if not storage_exists or not is_directory:
        missing_paths.append(str(actual))
    if not exports_exists:
        missing_paths.append(str(exports_path))
    if not runs_exists:
        missing_paths.append(str(runs_path))

    if missing_paths:
        return (
            StorageCheck(
                status="missing",
                expected_storage_path=str(expected),
                actual_storage_path=str(actual),
                storage_exists=storage_exists,
                is_directory=is_directory,
                exports_exists=exports_exists,
                runs_exists=runs_exists,
                missing_paths=tuple(missing_paths),
                reason="required_storage_paths_missing",
            ),
            ProjectState.STORAGE_MISSING,
        )

    return (
        StorageCheck(
            status="valid",
            expected_storage_path=str(expected),
            actual_storage_path=str(actual),
            storage_exists=True,
            is_directory=True,
            exports_exists=True,
            runs_exists=True,
            missing_paths=(),
            reason="storage_valid",
        ),
        None,
    )


def _is_real_directory(path: Path) -> bool:
    """Accept only a directory at its lexical path, never a redirect."""

    try:
        if path.is_symlink() or path.is_junction() or not path.is_dir():
            return False
        return path.resolve(strict=True) == path.absolute()
    except OSError:
        return False
