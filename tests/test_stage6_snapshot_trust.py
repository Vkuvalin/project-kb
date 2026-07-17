import contextlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from _stage6_support import git, write_gold_v1_snapshot

import project_kb.indexing.scanner as scanner_module
from project_kb.errors import IndexingError, RegistryOperationError, SnapshotQueryError
from project_kb.indexing import capture as capture_module
from project_kb.indexing.models import IndexOutcome, ScanPolicy
from project_kb.indexing.module_map import (
    ModuleMap,
    ModuleStateInvariantCode,
    PackagingEvidence,
    SourceRootOrigin,
    build_module_map,
)
from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.repo_identity import repository_identity_hash
from project_kb.snapshot.database import SnapshotReader, validate_snapshot


def _write_src_package(repo: Path, *, package: str = "example_project") -> str:
    relative_path = f"src/{package}/module.py"
    package_root = repo / "src" / package
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package_root / "module.py").write_text(
        "from .helper import VALUE\nRESULT = VALUE\n",
        encoding="utf-8",
    )
    return relative_path


def _index_and_read_module(
    repo: Path,
    relative_path: str,
    *,
    service: IndexService | None = None,
) -> tuple[IndexOutcome, dict[str, Any], sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row]]:
    project = RegistryService().register("repo-one", repo).project
    assert project is not None
    outcome = (service or IndexService()).index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    metadata = validate_snapshot(database, project_id=project.project_id)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        module = connection.execute(
            "SELECT * FROM files WHERE relative_path = ?",
            (relative_path,),
        ).fetchone()
        symbols = connection.execute(
            "SELECT * FROM symbols WHERE file_id = ? ORDER BY start_line, start_column",
            (module["file_id"],),
        ).fetchall()
        imports = connection.execute(
            "SELECT * FROM imports WHERE file_id = ? ORDER BY start_line",
            (module["file_id"],),
        ).fetchall()
        schema_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'files'"
        ).fetchone()[0]

    assert outcome.data["snapshot"]["schema_version"] == 2
    assert outcome.data["publication"]["atomic"] is True
    assert metadata["build_status"] == "SEALED"
    assert metadata["snapshot_classification"] == "valid_v2"
    assert module is not None
    for origin in SourceRootOrigin:
        assert f"'{origin.value}'" in schema_sql
    return outcome, metadata, module, symbols, imports


def _assert_fail_closed_module(
    module: sqlite3.Row,
    symbols: list[sqlite3.Row],
    imports: list[sqlite3.Row],
    *,
    origin: SourceRootOrigin,
) -> None:
    assert module["source_root_id"] is None
    assert module["source_root_path"] is None
    assert module["source_root_origin"] == origin
    assert module["module_name"] is None
    assert module["module_resolution_status"] == "AMBIGUOUS"
    assert json.loads(module["module_candidates_json"]) == []
    assert module["is_importable"] == 0
    assert all(symbol["module_name"] is None for symbol in symbols)
    assert all(symbol["canonical_qualified_name"] is None for symbol in symbols)
    assert all(symbol["logical_key"] is None for symbol in symbols)
    assert all(item["v2_resolution_status"] == "AMBIGUOUS" for item in imports)
    assert all(item["v2_resolved_file_id"] is None for item in imports)


def test_hand_authored_v1_snapshot_is_valid_legacy_and_queryable(temp_git_repo: Path) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )

    metadata = validate_snapshot(database, project_id=project.project_id)
    symbols = SnapshotReader(database, project_id=project.project_id).symbols()

    assert metadata["schema_version"] == 1
    assert metadata["snapshot_classification"] == "valid_v1"
    assert metadata["compatibility"]["schema_compatible"] is True
    assert [item["qualified_name"] for item in symbols] == ["legacy", "legacy.legacy"]


def test_v1_compatibility_is_pinned_independently_from_future_policy_defaults(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )

    metadata = validate_snapshot(
        database,
        project_id=project.project_id,
        expected_policy_version="future-v2-policy",
    )

    assert metadata["snapshot_classification"] == "valid_v1"
    assert metadata["compatibility"]["policy_compatible"] is True


def test_v1_only_reads_never_mutate_or_semantically_migrate_the_snapshot(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )
    original = database.read_bytes()

    validate_snapshot(database, project_id=project.project_id)
    SnapshotReader(database, project_id=project.project_id).symbols()

    assert database.read_bytes() == original


def test_v2_only_validation_on_valid_v1_requires_full_reindex(temp_git_repo: Path) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id, require_v2=True)

    assert captured.value.code == "SNAPSHOT_REBUILD_REQUIRED"
    assert captured.value.details == {
        "snapshot_classification": "valid_v1",
        "reason": "legacy_v1_reindex_required",
        "full_reindex_required": True,
    }


def test_unknown_snapshot_version_is_incompatible_rather_than_corrupt(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE snapshot_meta SET schema_version = 99")
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_REBUILD_REQUIRED"
    assert captured.value.details["snapshot_classification"] == "incompatible_version"


def test_damaged_hand_authored_v1_is_corrupt_not_incompatible(temp_git_repo: Path) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE relations")
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"


def test_explicit_currentness_on_valid_v1_is_unverified_and_requires_reindex(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )

    legacy = ProjectStatusService().status("repo-one")
    status = ProjectStatusService().status(
        "repo-one",
        verification_mode="strong",
    )

    assert legacy.snapshot_check.availability == "AVAILABLE"
    assert legacy.snapshot_check.compatibility == "LEGACY_V1"
    assert legacy.snapshot_check.currentness == "UNVERIFIED"
    assert legacy.availability.can_use_snapshot is True
    assert status.snapshot_check.availability == "AVAILABLE"
    assert status.snapshot_check.compatibility == "LEGACY_V1"
    assert status.snapshot_check.currentness == "UNVERIFIED"
    assert status.snapshot_check.reason == "legacy_v1_reindex_required_for_currentness"
    assert status.snapshot_check.verification_mode == "strong"
    assert status.snapshot_check.verification_duration_ms is None
    assert status.snapshot_check.verification_timings_ms == {}
    assert status.snapshot_check.verified_at is None
    assert status.project_state == "SNAPSHOT_LEGACY_REINDEX_REQUIRED"
    assert status.availability.can_use_snapshot is True
    assert status.availability.can_search is False
    assert status.requires_user_action is True
    assert status.recommended_action.code == "REINDEX_FOR_V2_CURRENTNESS"
    assert status.recommended_action.command == "pkb index repo-one --full --json"


def test_full_reindex_builds_versioned_v2_proof_and_occurrence_foundation(
    temp_git_repo: Path,
) -> None:
    package = temp_git_repo / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (temp_git_repo / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None

    outcome = IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    metadata = validate_snapshot(
        database,
        project_id=project.project_id,
        expected_repo_root_norm=project.repo_root_norm,
    )

    assert outcome.data["snapshot"]["schema_version"] == 2
    assert metadata["schema_version"] == 2
    assert metadata["snapshot_classification"] == "valid_v2"
    assert metadata["repository_identity_hash"]
    assert metadata["repository_binding_generation"] == project.repo_binding_generation
    assert metadata["proof_contract_version"]
    with sqlite3.connect(database) as connection:
        proof_count = connection.execute("SELECT COUNT(*) FROM proof_manifest").fetchone()[0]
        module_row = connection.execute(
            "SELECT module_name, source_root_path FROM files WHERE relative_path = ?",
            ("src/pkg/mod.py",),
        ).fetchone()
        occurrence = connection.execute(
            """SELECT s.symbol_id, s.legacy_symbol_id, s.canonical_qualified_name
               FROM symbols s JOIN files f ON f.file_id = s.file_id
               WHERE f.relative_path = ? AND s.short_name = ?""",
            ("src/pkg/mod.py", "value"),
        ).fetchone()
    assert proof_count >= 4
    assert module_row == ("pkg.mod", "src")
    assert occurrence[0] != occurrence[1]
    assert occurrence[2] == "pkg.mod.value"


def test_full_lifecycle_absent_packaging_uses_convention_exact(temp_git_repo: Path) -> None:
    relative_path = _write_src_package(temp_git_repo)

    _, _, module, _, _ = _index_and_read_module(temp_git_repo, relative_path)

    assert module["source_root_origin"] == SourceRootOrigin.CONVENTION
    assert module["module_resolution_status"] == "EXACT"
    assert module["module_name"] == "example_project.module"
    assert module["is_importable"] == 1


def test_full_lifecycle_supported_packaging_with_root_is_exact(temp_git_repo: Path) -> None:
    relative_path = _write_src_package(temp_git_repo)
    (temp_git_repo / "pyproject.toml").write_text(
        """[project]
name = "example-project"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
""",
        encoding="utf-8",
    )

    _, _, module, _, _ = _index_and_read_module(temp_git_repo, relative_path)

    assert module["source_root_origin"] == SourceRootOrigin.PYPROJECT
    assert module["source_root_path"] == "src"
    assert module["module_resolution_status"] == "EXACT"
    assert module["module_name"] == "example_project.module"


def test_full_lifecycle_supported_packaging_without_root_is_valid_non_exact(
    temp_git_repo: Path,
) -> None:
    relative_path = _write_src_package(temp_git_repo)
    (temp_git_repo / "pyproject.toml").write_text(
        '[project]\nname = "example-project"\n',
        encoding="utf-8",
    )

    _, _, module, symbols, imports = _index_and_read_module(temp_git_repo, relative_path)

    _assert_fail_closed_module(
        module,
        symbols,
        imports,
        origin=SourceRootOrigin.PACKAGING_EVIDENCE_SUPPORTED,
    )


def test_full_lifecycle_uv_build_reference_shape_is_valid_non_exact(
    temp_git_repo: Path,
) -> None:
    relative_path = _write_src_package(temp_git_repo)
    (temp_git_repo / "pyproject.toml").write_text(
        """[project]
name = "example-project"

[build-system]
requires = ["uv_build"]
build-backend = "uv_build"
""",
        encoding="utf-8",
    )

    outcome, _, module, symbols, imports = _index_and_read_module(
        temp_git_repo,
        relative_path,
    )

    _assert_fail_closed_module(
        module,
        symbols,
        imports,
        origin=SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED,
    )
    database = Path(outcome.data["project"]["storage_path"]) / "kb.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        convention_exact_count = connection.execute(
            """SELECT COUNT(*) FROM files
               WHERE language = 'python' AND source_root_origin = 'CONVENTION'
                     AND module_resolution_status = 'EXACT'"""
        ).fetchone()[0]
    assert outcome.code in {"INDEX_PUBLISHED", "INDEX_PUBLISHED_WITH_WARNINGS"}
    assert outcome.data["timings"]["total_ms"] >= 0
    assert convention_exact_count == 0


@pytest.mark.parametrize(
    ("marker", "contents"),
    [
        ("setup.cfg", "[options]\npackage_dir =\n    = src\n"),
        (
            "setup.py",
            "from pathlib import Path\nPath('setup-executed.txt').write_text('unsafe')\n",
        ),
    ],
)
def test_full_lifecycle_setup_evidence_is_valid_non_exact_without_execution(
    temp_git_repo: Path,
    marker: str,
    contents: str,
) -> None:
    relative_path = _write_src_package(temp_git_repo)
    (temp_git_repo / marker).write_text(contents, encoding="utf-8")

    _, _, module, symbols, imports = _index_and_read_module(temp_git_repo, relative_path)

    _assert_fail_closed_module(
        module,
        symbols,
        imports,
        origin=SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED,
    )
    assert not (temp_git_repo / "setup-executed.txt").exists()


def test_full_lifecycle_unreadable_packaging_is_valid_non_exact(temp_git_repo: Path) -> None:
    relative_path = _write_src_package(temp_git_repo)
    (temp_git_repo / "pyproject.toml").write_text(
        '[build-system]\nbuild-backend = "setuptools.build_meta"\n' + "# bounded metadata\n" * 20,
        encoding="utf-8",
    )
    service = IndexService()
    service.policy = ScanPolicy(max_text_bytes=64)

    _, _, module, symbols, imports = _index_and_read_module(
        temp_git_repo,
        relative_path,
        service=service,
    )

    _assert_fail_closed_module(
        module,
        symbols,
        imports,
        origin=SourceRootOrigin.PACKAGING_EVIDENCE_UNREADABLE,
    )


def test_full_lifecycle_explicit_invalid_authority_is_valid_non_exact(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative_path = _write_src_package(temp_git_repo)

    def build_explicit_invalid_module_map(
        relative_paths: Iterable[str],
        *,
        packaging_evidence: PackagingEvidence,
    ) -> ModuleMap:
        return build_module_map(
            relative_paths,
            explicit_source_roots=(),
            packaging_evidence=packaging_evidence,
        )

    monkeypatch.setattr(
        scanner_module,
        "build_module_map",
        build_explicit_invalid_module_map,
    )

    _, _, module, symbols, imports = _index_and_read_module(temp_git_repo, relative_path)

    _assert_fail_closed_module(
        module,
        symbols,
        imports,
        origin=SourceRootOrigin.EXPLICIT_INVALID,
    )


@pytest.mark.parametrize(
    ("mutation", "invariant_code"),
    [
        (
            """UPDATE files SET source_root_origin = 'FORGED'
               WHERE relative_path = 'src/example_project/module.py'""",
            ModuleStateInvariantCode.SOURCE_ROOT_INVALID,
        ),
        (
            f"""UPDATE files
                SET source_root_id = NULL, source_root_path = NULL,
                    source_root_origin = '{SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED}',
                    module_name = 'forged.module', module_resolution_status = 'AMBIGUOUS',
                    module_candidates_json = '[]', is_importable = 0
                WHERE relative_path = 'src/example_project/module.py'""",
            ModuleStateInvariantCode.NON_EXACT_CANONICAL_FIELDS_INVALID,
        ),
        (
            """UPDATE files SET source_root_id = NULL
               WHERE relative_path = 'src/example_project/module.py'""",
            ModuleStateInvariantCode.SOURCE_ROOT_INVALID,
        ),
        (
            """UPDATE files SET module_resolution_status = 'AMBIGUOUS'
               WHERE relative_path = 'src/example_project/module.py'""",
            ModuleStateInvariantCode.NON_EXACT_CANONICAL_FIELDS_INVALID,
        ),
        (
            """UPDATE files SET module_candidates_json = '{"shape":"forged"}'
               WHERE relative_path = 'src/example_project/module.py'""",
            ModuleStateInvariantCode.CANDIDATES_INVALID,
        ),
    ],
)
def test_invalid_persisted_module_combinations_have_bounded_reason_codes(
    temp_git_repo: Path,
    mutation: str,
    invariant_code: str,
) -> None:
    _write_src_package(temp_git_repo)
    (temp_git_repo / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(mutation)
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"
    assert captured.value.details == {
        "validation_area": "MODULE_MAP",
        "invariant_code": invariant_code,
    }
    assert captured.value.__cause__ is not None


@pytest.mark.parametrize(
    ("mutation", "validation_area", "invariant_code"),
    [
        (
            """UPDATE symbols SET occurrence_ordinal = occurrence_ordinal + 1
               WHERE symbol_id = (SELECT symbol_id FROM symbols LIMIT 1)""",
            "SYMBOL_OCCURRENCE",
            "SYMBOL_OCCURRENCE_ID_INVALID",
        ),
        (
            "UPDATE imports SET v2_resolution_status = 'FORGED'",
            "IMPORT_TARGET",
            "IMPORT_TARGET_CONTRACT_INVALID",
        ),
    ],
)
def test_occurrence_and_import_validation_have_bounded_reason_codes(
    temp_git_repo: Path,
    mutation: str,
    validation_area: str,
    invariant_code: str,
) -> None:
    _write_src_package(temp_git_repo)
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(mutation)
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"
    assert captured.value.details == {
        "validation_area": validation_area,
        "invariant_code": invariant_code,
    }
    assert captured.value.__cause__ is not None


def test_incompatible_v2_proof_contract_is_rebuild_required_not_current(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE snapshot_meta SET proof_contract_version = 'unknown'")
        connection.commit()

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.project_state == "SNAPSHOT_REBUILD_REQUIRED"
    assert status.snapshot_check.availability == "INCOMPATIBLE"
    assert status.snapshot_check.compatibility == "INCOMPATIBLE"
    assert status.snapshot_check.currentness == "UNVERIFIED"


def test_damaged_v2_proof_manifest_is_corrupt(temp_git_repo: Path) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE proof_manifest")
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"


@pytest.mark.parametrize(
    "mutation",
    [
        """UPDATE proof_manifest
           SET content_hash = '0000000000000000000000000000000000000000000000000000000000000000'
           WHERE proof_class = 'CONTENT_HASH'""",
        """UPDATE proof_manifest
           SET proof_class = 'EXCLUDED_HARD_SECRET', content_hash = NULL,
               verification_scope = 'EXCLUDED', exclusion_reason = 'forged'
           WHERE proof_class = 'CONTENT_HASH'""",
        "UPDATE snapshot_meta SET policy_json = '{damaged-json'",
    ],
)
def test_damaged_v2_proof_rows_are_corrupt(
    temp_git_repo: Path,
    mutation: str,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"


@pytest.mark.parametrize(
    "mutation",
    [
        """UPDATE symbols SET occurrence_ordinal = occurrence_ordinal + 1
           WHERE symbol_id = (SELECT symbol_id FROM symbols LIMIT 1)""",
        "UPDATE files SET module_resolution_status = 'FORGED'",
        """UPDATE files SET module_resolution_status = 'AMBIGUOUS'
           WHERE module_resolution_status = 'NOT_IMPORTABLE'""",
        'UPDATE files SET module_candidates_json = \'{"not": "a-list"}\'',
        """UPDATE files
           SET source_root_origin = 'FORGED',
               source_root_id = 'ffffffffffffffffffffffffffffffff'
           WHERE module_resolution_status = 'EXACT'""",
        "UPDATE imports SET v2_resolution_status = 'FORGED'",
        """UPDATE snapshot_meta
           SET repository_identity_hash =
               'z' || substr(repository_identity_hash, 2)""",
    ],
)
def test_damaged_v2_identity_and_module_contracts_are_corrupt(
    temp_git_repo: Path,
    mutation: str,
) -> None:
    (temp_git_repo / "module.py").write_text("import os\nVALUE = 1\n", encoding="utf-8")
    package = temp_git_repo / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("PACKAGE = True\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(mutation)
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"


def test_status_exposes_damaged_v2_as_corrupt_without_running_verifier(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE snapshot_meta SET policy_json = '{damaged-json'")
        connection.commit()

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.project_state == "SNAPSHOT_STORAGE_ERROR"
    assert status.snapshot_check.availability == "CORRUPT"
    assert status.snapshot_check.currentness == "UNVERIFIED"
    assert status.snapshot_check.verification_mode is None


def test_repository_state_json_cannot_mask_a_head_change(
    temp_git_repo: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "head-a")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    git(
        temp_git_repo,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-m",
        "head-b",
    )
    current_head = git(temp_git_repo, "rev-parse", "HEAD").stdout.decode().strip()
    database = Path(project.storage_path) / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        state = json.loads(
            connection.execute("SELECT repo_state_after_json FROM snapshot_meta").fetchone()[0]
        )
        state["head"] = current_head
        connection.execute(
            "UPDATE snapshot_meta SET repo_state_after_json = ?",
            (json.dumps(state, sort_keys=True),),
        )
        connection.commit()

    with pytest.raises(SnapshotQueryError) as captured:
        validate_snapshot(database, project_id=project.project_id)
    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert captured.value.code == "SNAPSHOT_STORAGE_ERROR"
    assert status.snapshot_check.availability == "CORRUPT"
    assert status.snapshot_check.currentness == "UNVERIFIED"


def test_relink_never_exposes_snapshot_from_previous_repository(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    (temp_git_repo / "old_repo.py").write_text("OLD = True\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")

    RegistryService().relink("repo-one", second_temp_git_repo)
    status = ProjectStatusService().status("repo-one")

    assert status.project_state == "SNAPSHOT_REBUILD_REQUIRED"
    assert status.availability.can_use_snapshot is False
    assert status.snapshot_check.reason == "snapshot_repository_binding_mismatch"


def test_same_root_relink_keeps_old_v1_quarantined_after_failed_reindex(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )
    before = ProjectStatusService().status("repo-one")
    assert before.availability.can_use_snapshot is True

    relinked = RegistryService().relink("repo-one", temp_git_repo).project
    assert relinked is not None
    assert relinked.repo_binding_generation != relinked.snapshot_binding_generation

    def fail_v2_write(*args: object, **kwargs: object) -> tuple[str, str, int, int]:
        del args, kwargs
        raise IndexingError("induced v2 reindex failure")

    monkeypatch.setattr(capture_module, "write_snapshot", fail_v2_write)
    with pytest.raises(IndexingError):
        IndexService().index("repo-one")

    status = ProjectStatusService().status("repo-one")
    assert status.project_state == "SNAPSHOT_REBUILD_REQUIRED"
    assert status.availability.can_use_snapshot is False
    assert status.snapshot_check.reason == "snapshot_repository_binding_mismatch"


def test_v2_binding_remains_usable_when_post_publish_registry_outcome_fails(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    relinked = RegistryService().relink("repo-one", temp_git_repo).project
    assert relinked is not None
    service = IndexService()

    def fail_registry_outcome(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RegistryOperationError("induced ancillary registry failure")

    monkeypatch.setattr(service.registry, "record_index_outcome", fail_registry_outcome)
    outcome = service.index("repo-one")
    status = ProjectStatusService().status("repo-one")
    database = Path(relinked.storage_path) / "kb.sqlite"
    metadata = validate_snapshot(
        database,
        project_id=relinked.project_id,
        expected_repository_binding_generation=relinked.repo_binding_generation,
    )

    assert outcome.result == "success_with_warnings"
    assert status.availability.can_use_snapshot is True
    assert metadata["repository_binding_generation"] == relinked.repo_binding_generation


def test_snapshot_reader_rechecks_active_binding_after_relink(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
) -> None:
    registered = RegistryService().register("repo-one", temp_git_repo).project
    assert registered is not None
    IndexService().index("repo-one")
    database = Path(registered.storage_path) / "kb.sqlite"

    relinked = RegistryService().relink("repo-one", second_temp_git_repo).project
    assert relinked is not None
    with pytest.raises(SnapshotQueryError) as captured:
        SnapshotReader(
            database,
            project_id=relinked.project_id,
            expected_repo_root_norm=relinked.repo_root_norm,
            expected_repository_identity_hash=repository_identity_hash(
                relinked.repo_root_norm,
                relinked.repo_fingerprint_json,
            ),
        )

    assert captured.value.code == "SNAPSHOT_REBUILD_REQUIRED"
    assert captured.value.details["snapshot_classification"] == "wrong_repository_binding"


def test_snapshot_reader_rechecks_binding_after_query_rows_are_read(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    database = Path(project.storage_path) / "kb.sqlite"
    identity_hash = repository_identity_hash(
        project.repo_root_norm,
        project.repo_fingerprint_json,
    )
    calls = 0

    def active_binding() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls < 3:
            return project.repo_root_norm, identity_hash
        return "changed-root", "changed-identity"

    reader = SnapshotReader(
        database,
        project_id=project.project_id,
        expected_repo_root_norm=project.repo_root_norm,
        expected_repository_identity_hash=identity_hash,
        active_binding=active_binding,
    )

    with pytest.raises(SnapshotQueryError) as captured:
        reader.symbols()

    assert calls == 3
    assert captured.value.code == "SNAPSHOT_REBUILD_REQUIRED"
    assert captured.value.details["reason"] == "active_binding_changed_during_query"


def test_failed_v2_cutover_preserves_hand_authored_v1_canonical_bytes(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )
    original = database.read_bytes()

    def fail_v2_write(*args: object, **kwargs: object) -> tuple[str, str, int, int]:
        del args, kwargs
        raise IndexingError("induced v2 cutover failure")

    monkeypatch.setattr(capture_module, "write_snapshot", fail_v2_write)

    with pytest.raises(IndexingError):
        IndexService().index("repo-one")
    assert database.read_bytes() == original


def test_successful_v1_to_v2_cutover_is_one_replace_and_reindexes_source(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "source_only.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    database = Path(project.storage_path) / "kb.sqlite"
    write_gold_v1_snapshot(
        database,
        project_id=project.project_id,
        repo_root_norm=project.repo_root_norm,
    )
    from project_kb.indexing import service as service_module

    real_publish = service_module.publish_snapshot
    calls = 0

    def count_publish(temp_path: Path, current_path: Path) -> bool:
        nonlocal calls
        calls += 1
        return real_publish(temp_path, current_path)

    monkeypatch.setattr(service_module, "publish_snapshot", count_publish)

    IndexService().index("repo-one")

    assert calls == 1
    with contextlib.closing(sqlite3.connect(database)) as connection:
        version = connection.execute("SELECT schema_version FROM snapshot_meta").fetchone()[0]
        paths = {row[0] for row in connection.execute("SELECT relative_path FROM files")}
    assert version == 2
    assert "source_only.py" in paths
    assert "legacy.py" not in paths
