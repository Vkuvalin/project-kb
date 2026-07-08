"""SQLite connection helpers for the Project KB registry."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from project_kb.errors import RegistryOperationError
from project_kb.registry.schema import initialize_schema
from project_kb.storage.home import resolve_home
from project_kb.version import __version__


def registry_path(home: Path | None = None) -> Path:
    return (home or resolve_home()) / "registry.sqlite"


@contextmanager
def open_registry(
    *,
    home: Path | None = None,
    now: Callable[[], str],
    event_id: Callable[[], str],
) -> Iterator[sqlite3.Connection]:
    resolved_home = home or resolve_home()
    db_path = registry_path(resolved_home)

    try:
        resolved_home.mkdir(parents=True, exist_ok=True)
        (resolved_home / "projects").mkdir(exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        initialize_schema(conn, now=now, tool_version=__version__, event_id=event_id)
        conn.commit()
        yield conn
        conn.commit()
    except OSError as exc:
        raise RegistryOperationError(
            "Registry storage directory could not be prepared.",
            details={"registry_path": str(db_path), "os_error": str(exc)},
        ) from exc
    except sqlite3.Error as exc:
        raise RegistryOperationError(
            "Registry operation failed.",
            details={"registry_path": str(db_path), "sqlite_error": str(exc)},
        ) from exc
    finally:
        with suppress(UnboundLocalError):
            conn.close()
