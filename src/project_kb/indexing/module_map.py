"""Canonical v2 source-root and Python module mapping."""

import hashlib
import keyword
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from project_kb.indexing.policy import normalize_relative_path, path_key

MODULE_MAP_VERSION = "4"
_SUPPORTED_BUILD_BACKENDS = {
    "hatchling.build",
    "setuptools.build_meta",
    "setuptools.build_meta:__legacy__",
}
_UNSUPPORTED_PACKAGING_TOOL_SECTIONS = {"flit", "pdm", "poetry"}


class ModuleResolutionStatus(StrEnum):
    EXACT = "EXACT"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_IMPORTABLE = "NOT_IMPORTABLE"


class SourceRootOrigin(StrEnum):
    """Versioned persisted vocabulary for exact and fail-closed root evidence."""

    EXPLICIT = "EXPLICIT"
    PYPROJECT = "PYPROJECT"
    CONVENTION = "CONVENTION"
    EXPLICIT_INVALID = "EXPLICIT_INVALID"
    PACKAGING_EVIDENCE_SUPPORTED = "PACKAGING_EVIDENCE_SUPPORTED"
    PACKAGING_EVIDENCE_UNSUPPORTED = "PACKAGING_EVIDENCE_UNSUPPORTED"
    PACKAGING_EVIDENCE_UNREADABLE = "PACKAGING_EVIDENCE_UNREADABLE"


SOURCE_ROOT_ORIGIN_CONTRACT_VERSION = MODULE_MAP_VERSION
EXACT_SOURCE_ROOT_ORIGINS = frozenset(
    {
        SourceRootOrigin.EXPLICIT,
        SourceRootOrigin.PYPROJECT,
        SourceRootOrigin.CONVENTION,
    }
)
FAIL_CLOSED_SOURCE_ROOT_ORIGINS = frozenset(
    {
        SourceRootOrigin.EXPLICIT_INVALID,
        SourceRootOrigin.PACKAGING_EVIDENCE_SUPPORTED,
        SourceRootOrigin.PACKAGING_EVIDENCE_UNSUPPORTED,
        SourceRootOrigin.PACKAGING_EVIDENCE_UNREADABLE,
    }
)
SOURCE_ROOT_ORIGIN_VALUES = tuple(origin.value for origin in SourceRootOrigin)


class ModuleStateInvariantCode(StrEnum):
    CANDIDATES_INVALID = "MODULE_CANDIDATES_INVALID"
    STATUS_INVALID = "MODULE_RESOLUTION_STATUS_INVALID"
    SOURCE_ROOT_INVALID = "MODULE_SOURCE_ROOT_STATE_INVALID"
    EXACT_STATE_INVALID = "MODULE_EXACT_STATE_INVALID"
    EXACT_DUPLICATE_INVALID = "MODULE_EXACT_DUPLICATE_INVALID"
    NON_EXACT_CANONICAL_FIELDS_INVALID = "MODULE_NON_EXACT_CANONICAL_FIELDS_INVALID"
    NON_EXACT_STATE_INVALID = "MODULE_NON_EXACT_STATE_INVALID"


class PackagingEvidenceState(StrEnum):
    """Whether root packaging authority is absent, understood, unsafe, or unreadable."""

    ABSENT = "PACKAGING_EVIDENCE_ABSENT"
    SUPPORTED = "PACKAGING_EVIDENCE_SUPPORTED"
    UNSUPPORTED = "PACKAGING_EVIDENCE_UNSUPPORTED"
    UNREADABLE = "PACKAGING_EVIDENCE_UNREADABLE"


@dataclass(frozen=True)
class PackagingEvidence:
    state: PackagingEvidenceState
    markers: tuple[str, ...] = ()
    pyproject_text: str | None = None
    source_roots: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRoot:
    source_root_id: str
    path: str
    origin: str


@dataclass(frozen=True)
class ModuleIdentity:
    relative_path: str
    path_key: str
    source_root_id: str | None
    source_root_path: str | None
    source_root_origin: str | None
    module_name: str | None
    resolution_status: ModuleResolutionStatus
    module_candidates: tuple[str, ...]
    is_importable: bool


@dataclass(frozen=True)
class ModuleMap:
    entries: dict[str, ModuleIdentity]
    source_roots: tuple[SourceRoot, ...]
    packaging_evidence: PackagingEvidence
    version: str = MODULE_MAP_VERSION

    def for_path(self, relative_path: str) -> ModuleIdentity:
        normalized = normalize_relative_path(relative_path)
        try:
            return self.entries[path_key(normalized)]
        except KeyError:
            return ModuleIdentity(
                relative_path=normalized,
                path_key=path_key(normalized),
                source_root_id=None,
                source_root_path=None,
                source_root_origin=None,
                module_name=None,
                resolution_status=ModuleResolutionStatus.NOT_IMPORTABLE,
                module_candidates=(),
                is_importable=False,
            )


@dataclass(frozen=True)
class _PackagingSourceRoots:
    roots: tuple[str, ...]
    unsupported: bool = False


def build_module_map(
    relative_paths: Iterable[str],
    *,
    pyproject_text: str | None = None,
    explicit_source_roots: Iterable[str] | None = None,
    packaging_evidence: PackagingEvidence | None = None,
) -> ModuleMap:
    """Map observed Python paths using packaging evidence before conservative convention."""

    paths = tuple(
        sorted(
            {
                normalize_relative_path(path)
                for path in relative_paths
                if PurePosixPath(path).suffix.casefold() in {".py", ".pyi"}
            },
            key=path_key,
        )
    )
    evidence = packaging_evidence or packaging_evidence_from_pyproject_text(pyproject_text)
    if explicit_source_roots is not None:
        try:
            explicit_roots = _explicit_roots(explicit_source_roots)
        except TypeError, ValueError:
            return _unresolved_module_map(
                paths,
                origin=SourceRootOrigin.EXPLICIT_INVALID,
                packaging_evidence=evidence,
            )
        if not explicit_roots:
            return _unresolved_module_map(
                paths,
                origin=SourceRootOrigin.EXPLICIT_INVALID,
                packaging_evidence=evidence,
            )
        root_specs = tuple((root, SourceRootOrigin.EXPLICIT) for root in explicit_roots)
    else:
        if evidence.state is PackagingEvidenceState.ABSENT:
            root_specs = tuple(
                (root, SourceRootOrigin.CONVENTION) for root in _convention_roots(paths)
            )
        elif evidence.state is PackagingEvidenceState.SUPPORTED and evidence.source_roots:
            root_specs = tuple((root, SourceRootOrigin.PYPROJECT) for root in evidence.source_roots)
        else:
            return _unresolved_module_map(
                paths,
                origin=SourceRootOrigin(evidence.state.value),
                packaging_evidence=evidence,
            )
    source_roots = tuple(
        SourceRoot(
            source_root_id=source_root_id(root, origin),
            path=root,
            origin=origin,
        )
        for root, origin in root_specs
    )

    entries: dict[str, ModuleIdentity] = {}
    for relative_path in paths:
        candidates: list[tuple[SourceRoot, str]] = []
        for root in source_roots:
            candidate = _module_candidate(
                relative_path,
                root,
                all_paths=paths,
            )
            if candidate is not None:
                candidates.append((root, candidate))
        entries[path_key(relative_path)] = _identity_from_candidates(relative_path, candidates)

    module_to_paths: dict[str, set[str]] = {}
    for entry in entries.values():
        if entry.resolution_status is ModuleResolutionStatus.EXACT and entry.module_name:
            module_to_paths.setdefault(entry.module_name, set()).add(entry.path_key)
    collided = {module for module, mapped_paths in module_to_paths.items() if len(mapped_paths) > 1}
    if collided:
        for key, entry in tuple(entries.items()):
            if entry.module_name in collided:
                entries[key] = replace(
                    entry,
                    module_name=None,
                    resolution_status=ModuleResolutionStatus.AMBIGUOUS,
                    is_importable=False,
                )
    return ModuleMap(
        entries=entries,
        source_roots=source_roots,
        packaging_evidence=evidence,
    )


def absolute_import_target(
    source: ModuleIdentity,
    *,
    source_is_package: bool,
    module_text: str,
    relative_level: int,
) -> str | None:
    """Resolve import spelling only when the source has one canonical module context."""

    if relative_level == 0:
        return module_text
    if source.resolution_status is not ModuleResolutionStatus.EXACT or not source.module_name:
        return None
    package = source.module_name if source_is_package else source.module_name.rpartition(".")[0]
    parts = package.split(".") if package else []
    remove = max(0, relative_level - 1)
    if remove > len(parts):
        return None
    base = parts[: len(parts) - remove]
    if module_text:
        base.extend(module_text.split("."))
    return ".".join(base)


def _pyproject_source_roots(pyproject_text: str | None) -> _PackagingSourceRoots:
    if pyproject_text is None:
        return _PackagingSourceRoots(())
    try:
        payload = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("pyproject.toml could not be parsed for module mapping") from exc
    build_system = payload.get("build-system")
    if build_system is not None and not isinstance(build_system, dict):
        raise ValueError("pyproject.toml build-system must be a table")
    backend = build_system.get("build-backend") if isinstance(build_system, dict) else None
    if backend is not None and not isinstance(backend, str):
        raise ValueError("pyproject.toml build-backend must be a string")
    tool = payload.get("tool")
    if tool is not None and not isinstance(tool, dict):
        raise ValueError("pyproject.toml tool configuration must be a table")
    tool = tool if isinstance(tool, dict) else {}
    unsupported_backend = backend is not None and backend not in _SUPPORTED_BUILD_BACKENDS
    unsupported_tool = bool(_UNSUPPORTED_PACKAGING_TOOL_SECTIONS.intersection(tool))
    if unsupported_backend or (backend is None and unsupported_tool):
        return _PackagingSourceRoots((), unsupported=True)
    roots: list[str] = []
    setuptools = tool.get("setuptools")
    if setuptools is not None and not isinstance(setuptools, dict):
        raise ValueError("tool.setuptools must be a table")
    if isinstance(setuptools, dict):
        package_dir = setuptools.get("package-dir")
        if package_dir is not None and not isinstance(package_dir, dict):
            raise ValueError("tool.setuptools.package-dir must be a table")
        if isinstance(package_dir, dict):
            if set(package_dir) - {""}:
                return _PackagingSourceRoots((), unsupported=True)
            default_root = package_dir.get("")
            if default_root is not None and not isinstance(default_root, str):
                raise ValueError("the default setuptools package-dir must be a string")
            if isinstance(default_root, str):
                roots.append(_normalize_root(default_root))
        packages = setuptools.get("packages")
        if packages is not None and not isinstance(packages, dict):
            return _PackagingSourceRoots((), unsupported=True)
        if isinstance(packages, dict):
            find = packages.get("find")
            if find is not None and not isinstance(find, dict):
                raise ValueError("tool.setuptools.packages.find must be a table")
            if isinstance(find, dict):
                where = find.get("where")
                if where is not None and not (
                    isinstance(where, list) and all(isinstance(item, str) for item in where)
                ):
                    raise ValueError("setuptools package discovery roots must be strings")
                if isinstance(where, list):
                    roots.extend(_normalize_root(item) for item in where)
        if "py-modules" in setuptools:
            return _PackagingSourceRoots((), unsupported=True)
    hatch = tool.get("hatch")
    if hatch is not None and not isinstance(hatch, dict):
        raise ValueError("tool.hatch must be a table")
    if isinstance(hatch, dict):
        roots.extend(_hatch_roots(hatch))
    return _PackagingSourceRoots(
        tuple(sorted(set(roots), key=lambda value: (value.count("/"), path_key(value))))
    )


def packaging_evidence_from_pyproject_text(pyproject_text: str | None) -> PackagingEvidence:
    if pyproject_text is None:
        return PackagingEvidence(PackagingEvidenceState.ABSENT)
    try:
        packaging = _pyproject_source_roots(pyproject_text)
    except ValueError:
        return PackagingEvidence(
            PackagingEvidenceState.UNREADABLE,
            markers=("pyproject.toml",),
            pyproject_text=pyproject_text,
            diagnostics=("PYPROJECT_INVALID",),
        )
    if packaging.unsupported:
        return PackagingEvidence(
            PackagingEvidenceState.UNSUPPORTED,
            markers=("pyproject.toml",),
            pyproject_text=pyproject_text,
            diagnostics=("PYPROJECT_UNSUPPORTED",),
        )
    return PackagingEvidence(
        PackagingEvidenceState.SUPPORTED,
        markers=("pyproject.toml",),
        pyproject_text=pyproject_text,
        source_roots=packaging.roots,
    )


def _hatch_roots(hatch: dict[str, Any]) -> list[str]:
    build = hatch.get("build")
    if build is None:
        return []
    if not isinstance(build, dict):
        raise ValueError("tool.hatch.build must be a table")
    targets = build.get("targets")
    if targets is None:
        return []
    if not isinstance(targets, dict):
        raise ValueError("tool.hatch.build.targets must be a table")
    wheel = targets.get("wheel")
    if wheel is None:
        return []
    if not isinstance(wheel, dict):
        raise ValueError("tool.hatch.build.targets.wheel must be a table")
    if {"only-include", "sources"}.intersection(wheel):
        raise ValueError("unsupported Hatch source-root mapping")
    packages = wheel.get("packages")
    if packages is None:
        return []
    if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
        raise ValueError("Hatch wheel packages must be a list of strings")
    roots: list[str] = []
    for package in packages:
        normalized = _normalize_root(package)
        parent = PurePosixPath(normalized).parent.as_posix()
        roots.append("" if parent == "." else parent)
    return roots


def _explicit_roots(values: Iterable[str]) -> tuple[str, ...]:
    roots: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("explicit source roots must be strings")
        roots.add(_normalize_root(value))
    return tuple(sorted(roots, key=lambda value: (value.count("/"), path_key(value or "root"))))


def _unresolved_module_map(
    paths: tuple[str, ...],
    *,
    origin: SourceRootOrigin,
    packaging_evidence: PackagingEvidence,
) -> ModuleMap:
    entries = {
        path_key(relative_path): ModuleIdentity(
            relative_path=relative_path,
            path_key=path_key(relative_path),
            source_root_id=None,
            source_root_path=None,
            source_root_origin=origin,
            module_name=None,
            resolution_status=ModuleResolutionStatus.AMBIGUOUS,
            module_candidates=(),
            is_importable=False,
        )
        for relative_path in paths
    }
    return ModuleMap(
        entries=entries,
        source_roots=(),
        packaging_evidence=packaging_evidence,
    )


def _convention_roots(paths: tuple[str, ...]) -> tuple[str, ...]:
    path_set = set(paths)
    roots: set[str] = set()
    top_level_packages = {
        PurePosixPath(path).parts[0]
        for path in paths
        if len(PurePosixPath(path).parts) >= 2
        and path == f"{PurePosixPath(path).parts[0]}/__init__.py"
    }
    if top_level_packages:
        roots.add("")
    has_src_python = any(path.startswith("src/") for path in paths)
    if has_src_python and "src/__init__.py" not in path_set:
        roots.add("src")
    return tuple(sorted(roots, key=lambda value: (value != "", path_key(value or "root"))))


def _module_candidate(
    relative_path: str,
    root: SourceRoot,
    *,
    all_paths: tuple[str, ...],
) -> str | None:
    if root.path:
        prefix = f"{root.path}/"
        if not relative_path.startswith(prefix):
            return None
        inside = relative_path[len(prefix) :]
    else:
        inside = relative_path
    pure = PurePosixPath(inside)
    if pure.suffix.casefold() not in {".py", ".pyi"}:
        return None
    without_suffix = inside[: -len(pure.suffix)]
    if without_suffix.endswith("/__init__"):
        without_suffix = without_suffix[: -len("/__init__")]
    elif without_suffix == "__init__":
        return None
    parts = tuple(part for part in without_suffix.split("/") if part)
    if not parts or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
        return None
    if root.origin == SourceRootOrigin.CONVENTION and root.path == "" and len(parts) > 1:
        parent_parts = PurePosixPath(inside).parts[:-1]
        for index in range(1, len(parent_parts) + 1):
            initializer = "/".join((*parent_parts[:index], "__init__.py"))
            if initializer not in all_paths:
                return None
    return ".".join(parts)


def _identity_from_candidates(
    relative_path: str,
    candidates: list[tuple[SourceRoot, str]],
) -> ModuleIdentity:
    normalized = normalize_relative_path(relative_path)
    if len(candidates) == 1:
        root, module_name = candidates[0]
        return ModuleIdentity(
            relative_path=normalized,
            path_key=path_key(normalized),
            source_root_id=root.source_root_id,
            source_root_path=root.path,
            source_root_origin=root.origin,
            module_name=module_name,
            resolution_status=ModuleResolutionStatus.EXACT,
            module_candidates=(module_name,),
            is_importable=True,
        )
    if candidates:
        return ModuleIdentity(
            relative_path=normalized,
            path_key=path_key(normalized),
            source_root_id=None,
            source_root_path=None,
            source_root_origin=None,
            module_name=None,
            resolution_status=ModuleResolutionStatus.AMBIGUOUS,
            module_candidates=tuple(sorted({candidate for _, candidate in candidates})),
            is_importable=False,
        )
    return ModuleIdentity(
        relative_path=normalized,
        path_key=path_key(normalized),
        source_root_id=None,
        source_root_path=None,
        source_root_origin=None,
        module_name=None,
        resolution_status=ModuleResolutionStatus.NOT_IMPORTABLE,
        module_candidates=(),
        is_importable=False,
    )


def _normalize_root(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    return normalize_relative_path(normalized)


def module_state_invariant_code(
    *,
    source_root_id_value: object,
    source_root_path: object,
    source_root_origin: object,
    module_name: object,
    resolution_status: object,
    module_candidates: object,
    is_importable: object,
) -> str | None:
    """Return the bounded invariant violated by one persisted module-map row."""

    if (
        not isinstance(module_candidates, (list, tuple))
        or any(not isinstance(item, str) or not item for item in module_candidates)
        or len(module_candidates) != len(set(module_candidates))
    ):
        return ModuleStateInvariantCode.CANDIDATES_INVALID
    candidates = tuple(module_candidates)
    try:
        status = ModuleResolutionStatus(resolution_status)
    except TypeError, ValueError:
        return ModuleStateInvariantCode.STATUS_INVALID

    try:
        origin = None if source_root_origin is None else SourceRootOrigin(source_root_origin)
    except TypeError, ValueError:
        return ModuleStateInvariantCode.SOURCE_ROOT_INVALID
    root_path_is_canonical = False
    if isinstance(source_root_path, str):
        try:
            root_path_is_canonical = (
                source_root_path == ""
                or normalize_relative_path(source_root_path) == source_root_path
            )
        except ValueError:
            root_path_is_canonical = False
    root_is_exact = (
        isinstance(source_root_id_value, str)
        and bool(source_root_id_value)
        and root_path_is_canonical
        and origin in EXACT_SOURCE_ROOT_ORIGINS
        and source_root_id_value == source_root_id(source_root_path, origin)
    )
    unresolved_root_evidence = (
        source_root_id_value is None
        and source_root_path is None
        and origin in FAIL_CLOSED_SOURCE_ROOT_ORIGINS
    )
    no_root = source_root_id_value is None and source_root_path is None and origin is None
    if not (root_is_exact or unresolved_root_evidence or no_root):
        return ModuleStateInvariantCode.SOURCE_ROOT_INVALID

    if status is ModuleResolutionStatus.EXACT:
        if (
            not root_is_exact
            or not isinstance(module_name, str)
            or not module_name
            or is_importable != 1
            or candidates != (module_name,)
        ):
            return ModuleStateInvariantCode.EXACT_STATE_INVALID
        return None

    if module_name is not None or is_importable != 0:
        return ModuleStateInvariantCode.NON_EXACT_CANONICAL_FIELDS_INVALID
    if status is ModuleResolutionStatus.AMBIGUOUS:
        valid_ambiguous = (
            (unresolved_root_evidence and not candidates)
            or (root_is_exact and len(candidates) == 1)
            or (no_root and len(candidates) >= 2)
        )
        return None if valid_ambiguous else ModuleStateInvariantCode.NON_EXACT_STATE_INVALID
    if status is ModuleResolutionStatus.NOT_IMPORTABLE and no_root and not candidates:
        return None
    return ModuleStateInvariantCode.NON_EXACT_STATE_INVALID


def source_root_id(root: str, origin: str | SourceRootOrigin) -> str:
    payload = f"{MODULE_MAP_VERSION}\0{origin}\0{root}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]
