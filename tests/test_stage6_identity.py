from pathlib import Path

from project_kb.indexing.extractor import extract_python
from project_kb.indexing.models import ScanPolicy
from project_kb.indexing.module_map import (
    EXACT_SOURCE_ROOT_ORIGINS,
    FAIL_CLOSED_SOURCE_ROOT_ORIGINS,
    MODULE_MAP_VERSION,
    SOURCE_ROOT_ORIGIN_CONTRACT_VERSION,
    PackagingEvidenceState,
    SourceRootOrigin,
    build_module_map,
)
from project_kb.indexing.scanner import _module_names, git_candidates, scan_repository


def test_source_root_origin_contract_is_versioned_and_complete() -> None:
    assert SOURCE_ROOT_ORIGIN_CONTRACT_VERSION == MODULE_MAP_VERSION
    assert (
        frozenset(
            {
                SourceRootOrigin.EXPLICIT,
                SourceRootOrigin.PYPROJECT,
                SourceRootOrigin.CONVENTION,
            }
        )
        == EXACT_SOURCE_ROOT_ORIGINS
    )
    assert (
        frozenset(
            {
                SourceRootOrigin.EXPLICIT_INVALID,
                SourceRootOrigin.PACKAGING_EVIDENCE_SUPPORTED,
                SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED,
                SourceRootOrigin.PACKAGING_EVIDENCE_UNREADABLE,
            }
        )
        == FAIL_CLOSED_SOURCE_ROOT_ORIGINS
    )
    assert frozenset(SourceRootOrigin) == (
        EXACT_SOURCE_ROOT_ORIGINS | FAIL_CLOSED_SOURCE_ROOT_ORIGINS
    )


def test_v1_module_name_gap_is_frozen_as_legacy_not_a_v1_failure() -> None:
    symbols, _, _, diagnostics = extract_python(
        file_id="legacy-file",
        relative_path="src/pkg/mod.py",
        source="VALUE = 1\n",
        line_count=1,
    )

    assert diagnostics == []
    assert symbols[0].qualified_name == "src.pkg.mod"
    assert _module_names("src/pkg/mod.py") == {"src.pkg.mod", "pkg.mod"}


def test_v2_module_map_uses_explicit_packaging_source_root() -> None:
    mapping = build_module_map(
        ["src/pkg/__init__.py", "src/pkg/mod.py"],
        pyproject_text='[tool.setuptools.packages.find]\nwhere = ["src"]\n',
    )

    module = mapping.for_path("src/pkg/mod.py")
    assert module.module_name == "pkg.mod"
    assert module.source_root_path == "src"
    assert module.source_root_origin == SourceRootOrigin.PYPROJECT
    assert module.resolution_status == "EXACT"


def test_v2_module_map_does_not_invent_a_module_for_script() -> None:
    module = build_module_map(["scripts/tool.py"]).for_path("scripts/tool.py")

    assert module.module_name is None
    assert module.resolution_status == "NOT_IMPORTABLE"
    assert module.is_importable is False


def test_v2_module_map_preserves_real_top_level_src_package() -> None:
    mapping = build_module_map(["src/__init__.py", "src/tool.py"])

    assert mapping.for_path("src/tool.py").module_name == "src.tool"


def test_v2_module_map_preserves_overlapping_root_ambiguity() -> None:
    mapping = build_module_map(
        ["src/pkg/mod.py"],
        pyproject_text='[tool.setuptools.packages.find]\nwhere = ["src", "src/pkg"]\n',
    )
    module = mapping.for_path("src/pkg/mod.py")

    assert module.resolution_status == "AMBIGUOUS"
    assert set(module.module_candidates) == {"pkg.mod", "mod"}


def test_occurrence_identity_is_deterministic_only_inside_snapshot_boundary(tmp_path: Path) -> None:
    del tmp_path
    from project_kb.indexing.identity import occurrence_id

    values = {
        "relative_path": "src/pkg/mod.py",
        "entity_kind": "FUNCTION",
        "binding_role": "DECLARATION",
        "start_line": 4,
        "end_line": 5,
        "start_column": 0,
        "end_column": 12,
        "ordinal": 0,
    }
    first = occurrence_id(snapshot_id="a" * 32, **values)

    assert occurrence_id(snapshot_id="a" * 32, **values) == first
    assert occurrence_id(snapshot_id="b" * 32, **values) != first


def test_package_initializer_and_explicit_namespace_package_share_root_authority() -> None:
    mapping = build_module_map(
        ["src/pkg/__init__.py", "src/ns/deep/mod.py"],
        pyproject_text='[tool.setuptools]\npackage-dir = {"" = "src"}\n',
    )

    assert mapping.for_path("src/pkg/__init__.py").module_name == "pkg"
    assert mapping.for_path("src/ns/deep/mod.py").module_name == "ns.deep.mod"


def test_duplicate_modules_across_multiple_roots_remain_ambiguous() -> None:
    mapping = build_module_map(
        ["src/pkg/mod.py", "lib/pkg/mod.py"],
        pyproject_text='[tool.setuptools.packages.find]\nwhere = ["src", "lib"]\n',
    )

    assert mapping.for_path("src/pkg/mod.py").resolution_status == "AMBIGUOUS"
    assert mapping.for_path("lib/pkg/mod.py").resolution_status == "AMBIGUOUS"
    assert mapping.for_path("src/pkg/mod.py").module_candidates == ("pkg.mod",)


def test_invalid_packaging_configuration_never_falls_back_to_a_guess() -> None:
    mapping = build_module_map(
        ["src/pkg/mod.py"],
        pyproject_text="[tool.setuptools.packages.find\nwhere = ['src']",
    )

    module = mapping.for_path("src/pkg/mod.py")
    assert module.resolution_status == "AMBIGUOUS"
    assert mapping.packaging_evidence.state is PackagingEvidenceState.UNREADABLE
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNREADABLE
    assert module.module_name is None


def test_invalid_hatch_source_root_shape_never_falls_back_to_convention() -> None:
    mapping = build_module_map(
        ["src/pkg/__init__.py", "src/pkg/mod.py"],
        pyproject_text='[tool.hatch]\nbuild = "custom"\n',
    )

    module = mapping.for_path("src/pkg/mod.py")
    assert mapping.source_roots == ()
    assert module.resolution_status == "AMBIGUOUS"
    assert mapping.packaging_evidence.state is PackagingEvidenceState.UNREADABLE
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNREADABLE
    assert module.module_name is None


def test_unsupported_packaging_backend_with_src_layout_never_uses_convention() -> None:
    mapping = build_module_map(
        ["src/pkg/__init__.py", "src/pkg/mod.py"],
        pyproject_text="""
[build-system]
build-backend = "poetry.core.masonry.api"

[tool.poetry]
packages = [{include = "pkg", from = "src"}]
""",
    )

    module = mapping.for_path("src/pkg/mod.py")
    assert mapping.source_roots == ()
    assert module.resolution_status == "AMBIGUOUS"
    assert mapping.packaging_evidence.state is PackagingEvidenceState.UNSUPPORTED
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED
    assert module.module_name is None


def test_unsupported_packaging_backend_with_top_level_layout_never_uses_convention() -> None:
    mapping = build_module_map(
        ["pkg/__init__.py", "pkg/mod.py"],
        pyproject_text="""
[build-system]
build-backend = "flit_core.buildapi"

[tool.flit.module]
name = "pkg"
""",
    )

    module = mapping.for_path("pkg/mod.py")
    assert mapping.source_roots == ()
    assert module.resolution_status == "AMBIGUOUS"
    assert mapping.packaging_evidence.state is PackagingEvidenceState.UNSUPPORTED
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED
    assert module.module_name is None


def test_absent_packaging_evidence_allows_convention_fallback() -> None:
    module = build_module_map(
        ["src/pkg/__init__.py", "src/pkg/mod.py"],
    ).for_path("src/pkg/mod.py")

    assert module.resolution_status == "EXACT"
    assert module.source_root_origin == SourceRootOrigin.CONVENTION
    assert module.module_name == "pkg.mod"


def test_present_supported_pyproject_without_source_root_does_not_use_convention() -> None:
    mapping = build_module_map(
        ["src/pkg/__init__.py", "src/pkg/mod.py"],
        pyproject_text='[project]\nname = "pkg"\nversion = "1.0"\n',
    )

    module = mapping.for_path("src/pkg/mod.py")
    assert mapping.packaging_evidence.state is PackagingEvidenceState.SUPPORTED
    assert module.resolution_status == "AMBIGUOUS"
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_SUPPORTED
    assert module.module_name is None


def test_supported_packaging_backend_with_unambiguous_root_remains_exact() -> None:
    module = build_module_map(
        ["src/pkg/__init__.py", "src/pkg/mod.py"],
        pyproject_text="""
[build-system]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
""",
    ).for_path("src/pkg/mod.py")

    assert module.resolution_status == "EXACT"
    assert module.source_root_origin == SourceRootOrigin.PYPROJECT
    assert module.module_name == "pkg.mod"


def test_explicit_source_root_authority_overrides_packaging_and_convention() -> None:
    module = build_module_map(
        ["custom/pkg/__init__.py", "custom/pkg/mod.py", "src/pkg/mod.py"],
        explicit_source_roots=["custom"],
        pyproject_text="""
[build-system]
build-backend = "poetry.core.masonry.api"

[tool.poetry]
packages = [{include = "pkg", from = "src"}]
""",
    ).for_path("custom/pkg/mod.py")

    assert module.resolution_status == "EXACT"
    assert module.source_root_origin == SourceRootOrigin.EXPLICIT
    assert module.source_root_path == "custom"
    assert module.module_name == "pkg.mod"


def test_scanner_shadow_identity_uses_one_mapper_without_v1_name_drift(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )
    package = temp_git_repo / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "base.py").write_text("class Base:\n    pass\n", encoding="utf-8")
    (package / "mod.py").write_text(
        "from .base import Base\n\ndef value():\n    return Base\n",
        encoding="utf-8",
    )
    policy = ScanPolicy()
    candidates = git_candidates(temp_git_repo, policy)

    first = scan_repository(temp_git_repo, candidates, policy, snapshot_id="a" * 32)
    repeated = scan_repository(temp_git_repo, candidates, policy, snapshot_id="a" * 32)
    next_snapshot = scan_repository(temp_git_repo, candidates, policy, snapshot_id="b" * 32)

    files = {file.relative_path: file for file in first.files}
    assert files["src/pkg/mod.py"].module_name == "pkg.mod"
    assert files["src/pkg/mod.py"].source_root_path == "src"
    module = next(
        symbol
        for symbol in first.symbols
        if symbol.file_id == files["src/pkg/mod.py"].file_id and symbol.symbol_kind == "MODULE"
    )
    assert module.qualified_name == "src.pkg.mod"
    assert module.canonical_qualified_name == "pkg.mod"
    assert module.occurrence_id is not None
    assert [symbol.occurrence_id for symbol in first.symbols] == [
        symbol.occurrence_id for symbol in repeated.symbols
    ]
    assert [symbol.occurrence_id for symbol in first.symbols] != [
        symbol.occurrence_id for symbol in next_snapshot.symbols
    ]
    imported = next(item for item in first.imports if item.module_text == "base")
    assert imported.v2_resolution_status == "EXACT"
    assert imported.normalized_module_name == "pkg.base"
    assert imported.v2_resolved_file_occurrence_id == files["src/pkg/base.py"].file_occurrence_id


def test_scanner_fails_closed_for_setup_cfg_package_layout_authority(temp_git_repo: Path) -> None:
    (temp_git_repo / "setup.cfg").write_text(
        "[options]\npackage_dir =\n    = src\n",
        encoding="utf-8",
    )
    package = temp_git_repo / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    facts = scan_repository(temp_git_repo, git_candidates(temp_git_repo), ScanPolicy())
    module = next(item for item in facts.files if item.relative_path == "src/pkg/mod.py")

    assert facts.packaging_evidence_state == "PACKAGING_EVIDENCE_UNSUPPORTED"
    assert facts.packaging_evidence_markers == ("setup.cfg",)
    assert module.module_resolution_status == "AMBIGUOUS"
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED
    assert module.module_name is None


def test_scanner_fails_closed_for_setup_py_without_executing_it(temp_git_repo: Path) -> None:
    sentinel = temp_git_repo / "setup-executed.txt"
    (temp_git_repo / "setup.py").write_text(
        "from pathlib import Path\nPath('setup-executed.txt').write_text('bad')\n",
        encoding="utf-8",
    )
    package = temp_git_repo / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    facts = scan_repository(temp_git_repo, git_candidates(temp_git_repo), ScanPolicy())
    module = next(item for item in facts.files if item.relative_path == "pkg/mod.py")

    assert facts.packaging_evidence_state == "PACKAGING_EVIDENCE_UNSUPPORTED"
    assert facts.packaging_evidence_markers == ("setup.py",)
    assert module.module_resolution_status == "AMBIGUOUS"
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED
    assert module.module_name is None
    assert not sentinel.exists()


def test_scanner_treats_unreadable_pyproject_as_packaging_evidence(temp_git_repo: Path) -> None:
    (temp_git_repo / "pyproject.toml").write_text(
        "[build-system]\nbuild-backend = 'setuptools.build_meta'\n" + "# metadata\n" * 20,
        encoding="utf-8",
    )
    package = temp_git_repo / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    facts = scan_repository(
        temp_git_repo,
        git_candidates(temp_git_repo),
        ScanPolicy(max_text_bytes=64),
    )
    module = next(item for item in facts.files if item.relative_path == "src/pkg/mod.py")

    assert facts.packaging_evidence_state == "PACKAGING_EVIDENCE_UNREADABLE"
    assert facts.packaging_evidence_markers == ("pyproject.toml",)
    assert module.module_resolution_status == "AMBIGUOUS"
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNREADABLE
    assert module.module_name is None


def test_setup_cfg_blocks_supported_pyproject_convention_or_exact_fallback(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "pyproject.toml").write_text(
        "[build-system]\nbuild-backend = 'setuptools.build_meta'\n"
        "[tool.setuptools.packages.find]\nwhere = ['src']\n",
        encoding="utf-8",
    )
    (temp_git_repo / "setup.cfg").write_text(
        "[options]\npackage_dir =\n    = alternate\n",
        encoding="utf-8",
    )
    package = temp_git_repo / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    facts = scan_repository(temp_git_repo, git_candidates(temp_git_repo), ScanPolicy())
    module = next(item for item in facts.files if item.relative_path == "src/pkg/mod.py")

    assert facts.packaging_evidence_state == "PACKAGING_EVIDENCE_UNSUPPORTED"
    assert facts.packaging_evidence_markers == ("pyproject.toml", "setup.cfg")
    assert module.module_resolution_status == "AMBIGUOUS"
    assert module.source_root_origin == SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED
    assert module.module_name is None
