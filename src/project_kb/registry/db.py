"""SQLite connection helpers for the Project KB registry."""

import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from project_kb.errors import RegistryOperationError
from project_kb.registry.schema import initialize_schema
from project_kb.storage.home import resolve_home
from project_kb.version import __version__

REGISTRY_BUSY_TIMEOUT_MS = 1_000


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
        _ensure_safe_registry_file(db_path, allow_missing=True)
        conn = _connect_registry(db_path)
        initialize_schema(
            conn,
            now=now,
            tool_version=__version__,
            event_id=event_id,
            allow_create=True,
        )
        yield conn
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


@contextmanager
def open_existing_registry(
    *,
    home: Path | None = None,
    now: Callable[[], str],
    event_id: Callable[[], str],
) -> Iterator[sqlite3.Connection | None]:
    """Open an existing registry without creating home or registry paths.

    A supported existing registry may receive the approved additive schema migration.
    Missing registry files are represented by ``None`` so diagnostic callers can treat
    them as an empty registration set without mutating the filesystem.
    """

    resolved_home = home or resolve_home()
    db_path = registry_path(resolved_home)
    if not _ensure_safe_registry_file(db_path, allow_missing=True):
        yield None
        return

    try:
        uri = f"{db_path.absolute().as_uri()}?mode=rw"
        conn = _connect_registry(uri, uri=True)
        initialize_schema(
            conn,
            now=now,
            tool_version=__version__,
            event_id=event_id,
            allow_create=False,
        )
        yield conn
    except OSError as exc:
        raise RegistryOperationError(
            "Registry file could not be opened.",
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


@contextmanager
def immediate_registry_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Own one short registry mutation using bounded ``BEGIN IMMEDIATE`` locking."""

    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RegistryOperationError("Registry write boundary requires foreign-key enforcement.")
    if conn.in_transaction:
        raise RegistryOperationError(
            "Registry write boundary requires an idle connection.",
            details={"transaction_mode": "BEGIN IMMEDIATE"},
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _connect_registry(path: Path | str, *, uri: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path,
        uri=uri,
        timeout=REGISTRY_BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {REGISTRY_BUSY_TIMEOUT_MS}")
    return conn


def _ensure_safe_registry_file(db_path: Path, *, allow_missing: bool) -> bool:
    """Reject redirected or non-regular registry paths before SQLite opens them."""

    try:
        path_stat = db_path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise
    except OSError as exc:
        raise RegistryOperationError(
            "Registry path could not be inspected.",
            details={"registry_path": str(db_path), "os_error": str(exc)},
        ) from exc

    if db_path.is_symlink() or db_path.is_junction() or not stat.S_ISREG(path_stat.st_mode):
        raise RegistryOperationError(
            "Registry path must be a regular non-redirected file.",
            details={"registry_path": str(db_path)},
        )
    return True
