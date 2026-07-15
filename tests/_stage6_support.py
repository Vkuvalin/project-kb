import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path

from _git_support import git as git


def write_gold_v1_snapshot(path: Path, *, project_id: str, repo_root_norm: str) -> None:
    """Create a frozen valid-v1 fixture without importing production schema or writer code."""

    schema = (Path(__file__).parent / "fixtures" / "stage6" / "v1_snapshot.sql").read_text(
        encoding="utf-8"
    )
    snapshot_id = "1" * 32
    run_id = "2" * 32
    file_id = "3" * 32
    module_id = "4" * 32
    function_id = "5" * 32
    source = b"def legacy():\n    return 1\n"
    content_hash = hashlib.sha256(source).hexdigest()
    empty_hash = hashlib.sha256(b"").hexdigest()
    state = {
        "head": None,
        "branch": "main",
        "status_fingerprint": empty_hash,
        "candidate_fingerprint": hashlib.sha256(b"TRACKED\0legacy.py\0").hexdigest(),
    }
    created_at = "2026-01-01T00:00:00.000000Z"

    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.executescript(schema)
        connection.execute(
            """INSERT INTO snapshot_meta (
                   snapshot_id, project_id, run_id, schema_version, scanner_version,
                   policy_version, policy_json, extractor_versions_json, created_at,
                   repo_root_norm, git_head, git_branch, git_status_fingerprint,
                   repo_state_before_json, repo_state_after_json, build_status,
                   logical_fingerprint, observation_fingerprint
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                project_id,
                run_id,
                1,
                "stage4-v0-3",
                "stage4-v0-3",
                json.dumps(
                    {
                        "policy_version": "stage4-v0-3",
                        "max_text_bytes": 2 * 1024 * 1024,
                        "binary_probe_bytes": 8192,
                        "discovered_metadata_paths": [],
                        "discovered_pruned_roots": [
                            ".venv",
                            "venv",
                            "node_modules",
                            ".tox",
                            "__pycache__",
                        ],
                    },
                    sort_keys=True,
                ),
                json.dumps({"python-ast": "1"}, sort_keys=True),
                created_at,
                repo_root_norm,
                None,
                "main",
                empty_hash,
                json.dumps(state, sort_keys=True),
                json.dumps(state, sort_keys=True),
                "SEALED",
                "6" * 64,
                "7" * 64,
            ),
        )
        connection.execute(
            """INSERT INTO files (
                   file_id, snapshot_id, relative_path, path_key, git_population,
                   file_kind, language, extension, size_bytes, mtime_ns, line_count,
                   encoding, content_hash, analysis_level, classification_reason, parse_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id,
                snapshot_id,
                "legacy.py",
                "legacy.py",
                "TRACKED",
                "TEXT",
                "python",
                ".py",
                len(source),
                1,
                2,
                "UTF-8",
                content_hash,
                "TEXT_STRUCTURAL",
                "supported_safe_text",
                "SUCCESS",
            ),
        )
        connection.executemany(
            """INSERT INTO symbols (
                   symbol_id, snapshot_id, file_id, qualified_name, short_name,
                   symbol_kind, start_line, end_line, start_column, end_column,
                   parent_symbol_id, signature_text, extractor_name, extractor_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    module_id,
                    snapshot_id,
                    file_id,
                    "legacy",
                    "legacy",
                    "MODULE",
                    1,
                    2,
                    0,
                    None,
                    None,
                    None,
                    "python-ast",
                    "1",
                ),
                (
                    function_id,
                    snapshot_id,
                    file_id,
                    "legacy.legacy",
                    "legacy",
                    "FUNCTION",
                    1,
                    2,
                    0,
                    12,
                    module_id,
                    "def legacy()",
                    "python-ast",
                    "1",
                ),
            ],
        )
        connection.execute(
            """INSERT INTO index_runs (
                   run_id, project_id, started_at, finished_at, status, failure_code,
                   candidate_count, candidate_bytes, metadata_only_count, pruned_root_count,
                   text_file_count, parsed_file_count, parse_failure_count, symbol_count,
                   import_count, relation_count, enumeration_ms, classification_ms,
                   read_hash_ms, parse_ms, relations_ms, database_write_ms, total_ms,
                   snapshot_size_bytes, published_snapshot_id, warnings_json
               ) VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               )""",
            (
                run_id,
                project_id,
                created_at,
                created_at,
                "BUILD_SUCCEEDED",
                None,
                1,
                len(source),
                0,
                0,
                1,
                1,
                0,
                2,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                snapshot_id,
                "[]",
            ),
        )
        connection.commit()
