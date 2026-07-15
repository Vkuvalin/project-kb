"""Versioned logical and snapshot-bound occurrence identity helpers."""

import hashlib

from project_kb.indexing.policy import normalize_relative_path, path_key

OCCURRENCE_CONTRACT_VERSION = "2"
LOGICAL_IDENTITY_VERSION = "2"


def file_occurrence_id(snapshot_id: str, relative_path: str) -> str:
    return _identity(
        "file-occurrence",
        OCCURRENCE_CONTRACT_VERSION,
        snapshot_id,
        path_key(normalize_relative_path(relative_path)),
    )


def occurrence_id(
    *,
    snapshot_id: str,
    relative_path: str,
    entity_kind: str,
    binding_role: str,
    start_line: int,
    end_line: int,
    start_column: int,
    end_column: int | None,
    ordinal: int,
) -> str:
    """Return an exact occurrence identity with no cross-snapshot promise."""

    return _identity(
        "entity-occurrence",
        OCCURRENCE_CONTRACT_VERSION,
        snapshot_id,
        file_occurrence_id(snapshot_id, relative_path),
        entity_kind,
        binding_role,
        start_line,
        end_line,
        start_column,
        end_column,
        ordinal,
    )


def logical_symbol_key(
    *,
    module_name: str,
    lexical_name: str,
    entity_kind: str,
    binding_role: str,
) -> str:
    """Return a deterministic non-lineage key; duplicate occurrences may share it."""

    return _identity(
        "logical-symbol",
        LOGICAL_IDENTITY_VERSION,
        module_name,
        lexical_name,
        entity_kind,
        binding_role,
    )


def _identity(*parts: object) -> str:
    payload = "\0".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
