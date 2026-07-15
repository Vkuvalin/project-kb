"""Git-populated, bounded, read-only repository scanner."""

import contextlib
import hashlib
import os
import stat as stat_module
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from project_kb.git_utils import (
    GitIndexChangedError,
    TemporaryGitIndex,
    git_bytes,
    git_text,
    git_z,
    live_git_index_generation,
    temporary_git_index,
)
from project_kb.indexing.extractor import extract_python, stable_id
from project_kb.indexing.identity import file_occurrence_id
from project_kb.indexing.models import (
    Candidate,
    FileFact,
    ImportFact,
    ObjectEvidence,
    PrunedRootFact,
    RelationFact,
    RepoObservation,
    RepoState,
    ScanFacts,
    ScanPolicy,
)
from project_kb.indexing.module_map import (
    MODULE_MAP_VERSION,
    ModuleMap,
    ModuleResolutionStatus,
    PackagingEvidence,
    PackagingEvidenceState,
    absolute_import_target,
    build_module_map,
    packaging_evidence_from_pyproject_text,
)
from project_kb.indexing.policy import (
    hard_secret_reason,
    language_for,
    normalize_relative_path,
    path_key,
    pruned_root,
    supported_text,
)


class ScanError(RuntimeError):
    pass


class RepositoryChangedError(ScanError):
    """The observed repository object changed during a bounded scan operation."""


class UnsafePathError(ScanError):
    """The path is statically unsafe before content access begins."""


class FileAccessError(ScanError):
    """The file could not be opened or inspected without mutation evidence."""


class FileReadError(ScanError):
    """An ordinary read or descriptor operation failed."""


_PACKAGING_MARKERS = ("pyproject.toml", "setup.cfg", "setup.py")


def git_candidates(
    repo_root: Path,
    policy: ScanPolicy | None = None,
    *,
    index_view: TemporaryGitIndex | None = None,
) -> list[Candidate]:
    if index_view is None:
        tracked = _git_z(repo_root, "ls-files", "-z")
        untracked = _git_z(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    else:
        tracked = _git_z(repo_root, "ls-files", "-z", index_view=index_view)
        untracked = _git_z(
            repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            index_view=index_view,
        )
    population: dict[str, str] = {}
    for raw in tracked:
        path = normalize_relative_path(raw)
        population[path_key(path)] = "TRACKED"
    for raw in untracked:
        path = normalize_relative_path(raw)
        population.setdefault(path_key(path), "UNTRACKED")
    policy = policy or ScanPolicy()
    discovered = _policy_candidates(repo_root, policy)
    for candidate in discovered:
        population.setdefault(path_key(candidate.relative_path), candidate.population)
    by_key = {
        path_key(normalize_relative_path(raw)): normalize_relative_path(raw)
        for raw in tracked + untracked
    }
    for item in discovered:
        by_key.setdefault(path_key(item.relative_path), item.relative_path)
    return [Candidate(by_key[key], population[key]) for key in sorted(population)]


def capture_repo_state(
    repo_root: Path,
    candidates: list[Candidate] | None = None,
    policy: ScanPolicy | None = None,
) -> RepoState:
    visibility_flags_before = git_index_flagged_paths(repo_root)
    candidates = candidates if candidates is not None else git_candidates(repo_root, policy)
    head = _git_text(repo_root, "rev-parse", "--verify", "HEAD", allow_failure=True) or None
    branch = (
        _git_text(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
        or None
    )
    status = _git_bytes(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index_visibility_paths = git_index_flagged_paths(repo_root)
    candidate_payload = "".join(f"{c.population}\0{c.relative_path}\0" for c in candidates)
    return RepoState(
        head=head,
        branch=branch,
        status_fingerprint=hashlib.sha256(status).hexdigest(),
        candidate_fingerprint=hashlib.sha256(candidate_payload.encode("utf-8")).hexdigest(),
        visibility_flags_before=visibility_flags_before,
        index_visibility_paths=index_visibility_paths,
    )


def capture_repo_observation(
    repo_root: Path,
    policy: ScanPolicy | None = None,
    *,
    temporary_root: Path,
) -> RepoObservation:
    """Capture candidates and status through one visibility-neutral index generation."""

    try:
        with temporary_git_index(repo_root, temporary_root=temporary_root) as index_view:
            source_visibility_paths = git_index_flagged_paths(repo_root)
            if git_index_flagged_paths(repo_root, index_view=index_view):
                raise ScanError("temporary Git index visibility flags could not be neutralized")
            candidates = git_candidates(repo_root, policy, index_view=index_view)
            head = (
                _git_text(
                    repo_root,
                    "rev-parse",
                    "--verify",
                    "HEAD",
                    allow_failure=True,
                    index_view=index_view,
                )
                or None
            )
            branch = (
                _git_text(
                    repo_root,
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    allow_failure=True,
                    index_view=index_view,
                )
                or None
            )
            status = _git_bytes(
                repo_root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                index_view=index_view,
            )
            generation_after = live_git_index_generation(
                repo_root,
                expected_path=index_view.source_path,
            )
    except GitIndexChangedError as exc:
        raise RepositoryChangedError("live Git index changed during observation") from exc
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"Git repository inspection failed: {type(exc).__name__}") from exc

    candidate_payload = "".join(
        f"{candidate.population}\0{candidate.relative_path}\0" for candidate in candidates
    )
    state = RepoState(
        head=head,
        branch=branch,
        status_fingerprint=hashlib.sha256(status).hexdigest(),
        candidate_fingerprint=hashlib.sha256(candidate_payload.encode("utf-8")).hexdigest(),
        visibility_flags_before=source_visibility_paths,
        index_visibility_paths=source_visibility_paths,
    )
    return RepoObservation(
        state=state,
        candidates=tuple(candidates),
        index_generation_before=index_view.source_generation,
        index_generation_after=generation_after,
    )


def git_status_paths(repo_root: Path) -> tuple[str, ...]:
    """Return every path named by the bounded porcelain-v1 status observation."""

    payload = _git_bytes(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = payload.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4 or field[2:3] != b" ":
            raise ScanError("Git status returned an invalid porcelain record")
        status = field[:2]
        paths.append(field[3:].decode("utf-8", errors="surrogateescape"))
        if (b"R" in status or b"C" in status) and index < len(fields) and fields[index]:
            paths.append(fields[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    try:
        return tuple(sorted({normalize_relative_path(path) for path in paths}, key=path_key))
    except ValueError as exc:
        raise ScanError("Git status returned an unsafe repository-relative path") from exc


def git_index_flagged_paths(
    repo_root: Path,
    *,
    index_view: TemporaryGitIndex | None = None,
) -> tuple[str, ...]:
    """Return tracked paths whose index flags can hide working-tree changes."""

    payload = _git_bytes(repo_root, "ls-files", "-v", "-z", index_view=index_view)
    paths: list[str] = []
    for field in payload.split(b"\0"):
        if not field:
            continue
        if len(field) < 3 or field[1:2] != b" ":
            raise ScanError("Git ls-files returned an invalid index-flag record")
        tag = field[:1]
        if tag != b"H":
            paths.append(field[2:].decode("utf-8", errors="surrogateescape"))
    try:
        return tuple(sorted({normalize_relative_path(path) for path in paths}, key=path_key))
    except ValueError as exc:
        raise ScanError("Git index flags named an unsafe repository-relative path") from exc


def scan_repository(
    repo_root: Path,
    candidates: list[Candidate],
    policy: ScanPolicy,
    *,
    snapshot_id: str | None = None,
) -> ScanFacts:
    facts = ScanFacts(candidate_count=len(candidates))
    classification_ns = 0
    read_ns = 0
    parse_ns = 0
    pruned_seen: set[str] = set()
    python_sources: list[tuple[FileFact, str]] = []
    pyproject_text: str | None = None

    for candidate in candidates:
        start = time.perf_counter_ns()
        relative_path = candidate.relative_path
        secret_reason = hard_secret_reason(relative_path)
        root = pruned_root(relative_path, policy)
        classification_ns += time.perf_counter_ns() - start
        absolute = repo_root / Path(*PurePosixPath(relative_path).parts)
        file_id = stable_id(path_key(relative_path))
        extension = PurePosixPath(relative_path).suffix.casefold()
        if _has_redirected_component(repo_root, relative_path):
            if root:
                if path_key(root) not in pruned_seen:
                    pruned_seen.add(path_key(root))
                    facts.pruned_roots.append(
                        PrunedRootFact(
                            root,
                            "PRUNED_ROOT",
                            "redirected_pruned_root",
                            policy.policy_version,
                        )
                    )
                    facts.evidence[path_key(root)] = ObjectEvidence(
                        relative_path=root,
                        evidence_kind="REDIRECTED_PRUNED_ROOT",
                        stat_signature=None,
                    )
            else:
                _record_metadata_file(
                    facts,
                    file_id,
                    candidate,
                    extension,
                    None,
                    "REDIRECT",
                    "symlink_or_reparse_component",
                )
            continue

        stat = _safe_lstat(absolute)
        size = stat.st_size if stat else None
        mtime_ns = stat.st_mtime_ns if stat else None
        facts.candidate_bytes += size or 0
        if root:
            if path_key(root) not in pruned_seen:
                pruned_seen.add(path_key(root))
                root_path = repo_root / Path(*PurePosixPath(root).parts)
                root_stat = _safe_lstat(root_path)
                facts.pruned_roots.append(
                    PrunedRootFact(
                        root, "PRUNED_ROOT", "pruned_directory_policy", policy.policy_version
                    )
                )
                facts.evidence[path_key(root)] = ObjectEvidence(
                    relative_path=root,
                    evidence_kind="PRUNED_ROOT",
                    stat_signature=_stat_signature(root_stat),
                )
            continue

        if secret_reason:
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "HARD_SECRET",
                secret_reason,
            )
            continue
        if stat is None:
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                None,
                "MISSING",
                "missing_worktree_file",
            )
            continue
        if _is_redirect(absolute):
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "REDIRECT",
                "symlink_or_reparse_point",
            )
            continue
        if not stat_module.S_ISREG(stat.st_mode):
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "UNSUPPORTED",
                "not_regular_file",
            )
            continue
        if not supported_text(relative_path):
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "UNSUPPORTED",
                "unsupported_format",
            )
            continue
        if size is not None and size > policy.max_text_bytes:
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "LARGE",
                "text_size_limit_exceeded",
            )
            continue

        read_start = time.perf_counter_ns()
        raw, binary = _read_safe_file(
            repo_root,
            absolute,
            stat,
            policy,
            preclassified_safe=True,
        )
        if binary:
            read_ns += time.perf_counter_ns() - read_start
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "BINARY",
                "bounded_binary_probe",
            )
            continue
        assert raw is not None
        try:
            encoding = "UTF-8-BOM" if raw.startswith(b"\xef\xbb\xbf") else "UTF-8"
            source = raw.decode(
                "utf-8-sig" if encoding == "UTF-8-BOM" else "utf-8", errors="strict"
            )
        except UnicodeDecodeError:
            read_ns += time.perf_counter_ns() - read_start
            _record_metadata_file(
                facts,
                file_id,
                candidate,
                extension,
                stat,
                "UNSUPPORTED_ENCODING",
                "strict_utf8_decode_failed",
            )
            continue

        content_hash = hashlib.sha256(raw).hexdigest()
        line_count = _line_count(source)
        read_ns += time.perf_counter_ns() - read_start
        file_fact = FileFact(
            file_id=file_id,
            relative_path=relative_path,
            path_key=path_key(relative_path),
            git_population=candidate.population,
            file_kind="TEXT",
            language=language_for(relative_path),
            extension=extension,
            size_bytes=size,
            mtime_ns=mtime_ns,
            line_count=line_count,
            encoding=encoding,
            content_hash=content_hash,
            analysis_level="TEXT_STRUCTURAL",
            classification_reason="supported_safe_text",
            parse_status="NOT_APPLICABLE",
        )
        facts.files.append(file_fact)
        facts.evidence[file_fact.path_key] = ObjectEvidence(
            relative_path=relative_path,
            evidence_kind="TEXT",
            stat_signature=_stat_signature(stat),
            content_hash=content_hash,
        )
        if relative_path == "pyproject.toml":
            pyproject_text = source
        if file_fact.language == "python":
            python_sources.append((file_fact, source))

    python_paths = [file.relative_path for file, _ in python_sources]
    packaging_evidence = _root_packaging_evidence(repo_root, pyproject_text=pyproject_text)
    module_map = build_module_map(python_paths, packaging_evidence=packaging_evidence)
    facts.module_map_version = MODULE_MAP_VERSION
    facts.packaging_evidence_state = module_map.packaging_evidence.state.value
    facts.packaging_evidence_markers = module_map.packaging_evidence.markers
    for observed_file in facts.files:
        if snapshot_id is not None:
            observed_file.file_occurrence_id = file_occurrence_id(
                snapshot_id, observed_file.relative_path
            )
        if observed_file.language != "python":
            observed_file.module_resolution_status = ModuleResolutionStatus.NOT_IMPORTABLE.value
    for file_fact, source in python_sources:
        module_identity = module_map.for_path(file_fact.relative_path)
        file_fact.source_root_id = module_identity.source_root_id
        file_fact.source_root_path = module_identity.source_root_path
        file_fact.source_root_origin = module_identity.source_root_origin
        file_fact.module_name = module_identity.module_name
        file_fact.module_resolution_status = module_identity.resolution_status.value
        file_fact.module_candidates = module_identity.module_candidates
        file_fact.is_importable = module_identity.is_importable
        parse_start = time.perf_counter_ns()
        symbols, imports, relations, diagnostics = extract_python(
            file_id=file_fact.file_id,
            relative_path=file_fact.relative_path,
            source=source,
            line_count=file_fact.line_count or 0,
            snapshot_id=snapshot_id,
            module_identity=module_identity,
        )
        parse_ns += time.perf_counter_ns() - parse_start
        file_fact.parse_status = "FAILED" if diagnostics else "SUCCESS"
        facts.symbols.extend(symbols)
        facts.imports.extend(imports)
        facts.relations.extend(relations)
        facts.diagnostics.extend(diagnostics)

    relations_start = time.perf_counter_ns()
    _resolve_imports(facts, module_map=module_map)
    facts.timings.update(
        {
            "classification_ms": classification_ns // 1_000_000,
            "read_hash_ms": read_ns // 1_000_000,
            "parse_ms": parse_ns // 1_000_000,
            "relations_ms": (time.perf_counter_ns() - relations_start) // 1_000_000,
        }
    )
    return facts


def _root_packaging_evidence(
    repo_root: Path,
    *,
    pyproject_text: str | None,
) -> PackagingEvidence:
    """Classify root packaging markers without executing or broadly parsing them."""

    markers: list[str] = []
    unreadable_markers: list[str] = []
    for marker in _PACKAGING_MARKERS:
        path = repo_root / marker
        observed = _lstat_for_read(path)
        if observed is None:
            continue
        markers.append(marker)
        if not stat_module.S_ISREG(observed.st_mode) or _is_redirect(path):
            unreadable_markers.append(marker)

    marker_tuple = tuple(markers)
    if not marker_tuple:
        return PackagingEvidence(PackagingEvidenceState.ABSENT)
    if "pyproject.toml" in marker_tuple and pyproject_text is None:
        unreadable_markers.append("pyproject.toml")
    if unreadable_markers:
        return PackagingEvidence(
            PackagingEvidenceState.UNREADABLE,
            markers=marker_tuple,
            diagnostics=tuple(
                f"PACKAGING_MARKER_UNREADABLE:{marker}"
                for marker in sorted(set(unreadable_markers))
            ),
        )

    pyproject_evidence = (
        packaging_evidence_from_pyproject_text(pyproject_text)
        if "pyproject.toml" in marker_tuple
        else PackagingEvidence(PackagingEvidenceState.ABSENT)
    )
    if "setup.cfg" in marker_tuple or "setup.py" in marker_tuple:
        diagnostics = list(pyproject_evidence.diagnostics)
        if "setup.cfg" in marker_tuple:
            diagnostics.append("SETUP_CFG_PRESENT")
        if "setup.py" in marker_tuple:
            diagnostics.append("SETUP_PY_PRESENT")
        return PackagingEvidence(
            PackagingEvidenceState.UNSUPPORTED,
            markers=marker_tuple,
            pyproject_text=pyproject_text,
            source_roots=pyproject_evidence.source_roots,
            diagnostics=tuple(diagnostics),
        )
    return PackagingEvidence(
        pyproject_evidence.state,
        markers=marker_tuple,
        pyproject_text=pyproject_evidence.pyproject_text,
        source_roots=pyproject_evidence.source_roots,
        diagnostics=pyproject_evidence.diagnostics,
    )


def _resolve_imports(facts: ScanFacts, *, module_map: ModuleMap | None = None) -> None:
    file_by_id = {file.file_id: file for file in facts.files}
    legacy_module_map: dict[str, set[str]] = {}
    legacy_local_roots: set[str] = set()
    for file in facts.files:
        if file.language != "python":
            continue
        for module in _module_names(file.relative_path):
            legacy_module_map.setdefault(module, set()).add(file.file_id)
            if module:
                legacy_local_roots.add(module.split(".", 1)[0])

    for item in facts.imports:
        source = file_by_id[item.file_id]
        target_module = _absolute_import_target(source.relative_path, item)
        possible = [target_module]
        if item.import_kind == "IMPORT_FROM" and item.imported_name not in {None, "*"}:
            possible.insert(0, f"{target_module}.{item.imported_name}".strip("."))
        matches: set[str] = set()
        for module in possible:
            matches = legacy_module_map.get(module, set())
            if matches:
                break
        if len(matches) == 1:
            item.resolution_status = "EXACT"
            item.resolved_file_id = next(iter(matches))
        elif len(matches) > 1:
            item.resolution_status = "AMBIGUOUS"
        elif item.relative_level or target_module.split(".", 1)[0] in legacy_local_roots:
            item.resolution_status = "UNRESOLVED"
        else:
            item.resolution_status = "EXTERNAL"

        if module_map is not None:
            _resolve_v2_import(item, source=source, facts=facts, module_map=module_map)

        facts.relations.append(
            RelationFact(
                relation_id=stable_id(item.import_id, "module"),
                relation_kind="FILE_IMPORTS_MODULE",
                source_file_id=item.file_id,
                source_symbol_id=None,
                target_file_id=None,
                target_symbol_id=None,
                target_text=target_module,
                start_line=item.start_line,
                end_line=item.end_line,
                start_column=None,
                end_column=None,
                resolution_status="EXACT",
                evidence_kind="AST_IMPORT",
            )
        )
        if item.resolution_status == "EXACT":
            facts.relations.append(
                RelationFact(
                    relation_id=stable_id(item.import_id, "file", item.resolved_file_id),
                    relation_kind="FILE_IMPORTS_FILE",
                    source_file_id=item.file_id,
                    source_symbol_id=None,
                    target_file_id=item.resolved_file_id,
                    target_symbol_id=None,
                    target_text=target_module,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    start_column=None,
                    end_column=None,
                    resolution_status="EXACT",
                    evidence_kind="DETERMINISTIC_MODULE_MAP",
                )
            )


def _resolve_v2_import(
    item: ImportFact,
    *,
    source: FileFact,
    facts: ScanFacts,
    module_map: ModuleMap,
) -> None:
    source_identity = module_map.for_path(source.relative_path)
    target_module = absolute_import_target(
        source_identity,
        source_is_package=source.relative_path.endswith("/__init__.py"),
        module_text=item.module_text,
        relative_level=item.relative_level,
    )
    if target_module is None:
        item.v2_resolution_status = "AMBIGUOUS"
        return
    possible = [target_module]
    if item.import_kind == "IMPORT_FROM" and item.imported_name not in {None, "*"}:
        possible.insert(0, f"{target_module}.{item.imported_name}".strip("."))
    file_ids_by_module: dict[str, set[str]] = {}
    local_roots: set[str] = set()
    file_by_id = {file.file_id: file for file in facts.files}
    for file in facts.files:
        if file.module_name and file.module_resolution_status == ModuleResolutionStatus.EXACT.value:
            file_ids_by_module.setdefault(file.module_name, set()).add(file.file_id)
            local_roots.add(file.module_name.split(".", 1)[0])
        for candidate in file.module_candidates:
            if candidate:
                local_roots.add(candidate.split(".", 1)[0])
    matches: set[str] = set()
    normalized = target_module
    for possible_module in possible:
        matches = file_ids_by_module.get(possible_module, set())
        if matches:
            normalized = possible_module
            break
    item.normalized_module_name = normalized
    if len(matches) == 1:
        item.v2_resolution_status = "EXACT"
        item.v2_resolved_file_occurrence_id = file_by_id[next(iter(matches))].file_occurrence_id
    elif len(matches) > 1:
        item.v2_resolution_status = "AMBIGUOUS"
    elif item.relative_level or target_module.split(".", 1)[0] in local_roots:
        item.v2_resolution_status = "UNRESOLVED"
    else:
        item.v2_resolution_status = "EXTERNAL"


def _module_names(relative_path: str) -> set[str]:
    path = relative_path
    for suffix in (".pyi", ".py"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]
    module = path.replace("/", ".")
    names = {module}
    first, separator, remainder = module.partition(".")
    if separator and first in {"src", "lib"}:
        names.add(remainder)
    return {name for name in names if name}


def _absolute_import_target(relative_path: str, item: ImportFact) -> str:
    if item.relative_level == 0:
        return item.module_text
    current_names = sorted(
        _module_names(relative_path), key=lambda value: (value.startswith("src."), len(value))
    )
    current = current_names[0] if current_names else ""
    package = current if relative_path.endswith("/__init__.py") else current.rpartition(".")[0]
    parts = package.split(".") if package else []
    remove = max(0, item.relative_level - 1)
    base = parts[: len(parts) - remove] if remove <= len(parts) else []
    if item.module_text:
        base.extend(item.module_text.split("."))
    return ".".join(base)


def _record_metadata_file(
    facts: ScanFacts,
    file_id: str,
    candidate: Candidate,
    extension: str,
    stat_result: os.stat_result | None,
    file_kind: str,
    reason: str,
) -> None:
    fact = FileFact(
        file_id=file_id,
        relative_path=candidate.relative_path,
        path_key=path_key(candidate.relative_path),
        git_population=candidate.population,
        file_kind=file_kind,
        language=None,
        extension=extension,
        size_bytes=stat_result.st_size if stat_result else None,
        mtime_ns=stat_result.st_mtime_ns if stat_result else None,
        line_count=None,
        encoding=None,
        content_hash=None,
        analysis_level="METADATA_ONLY",
        classification_reason=reason,
        parse_status="NOT_APPLICABLE",
    )
    facts.files.append(fact)
    facts.evidence[fact.path_key] = ObjectEvidence(
        relative_path=fact.relative_path,
        evidence_kind=file_kind,
        stat_signature=_stat_signature(stat_result),
    )


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FileAccessError("candidate metadata could not be inspected") from exc


def _is_redirect(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError as exc:
        raise FileAccessError("candidate reparse metadata could not be inspected") from exc


def _has_redirected_component(repo_root: Path, relative_path: str) -> bool:
    current = repo_root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if _is_redirect(current):
            return True
    return False


def _has_redirected_parent(repo_root: Path, relative_path: str) -> bool:
    current = repo_root
    for part in PurePosixPath(relative_path).parts[:-1]:
        current = current / part
        if _is_redirect(current):
            return True
    return False


def _validated_parent_components(
    repo_root: Path, relative_path: str
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    """Return stable lstat evidence for every parent without following redirects."""
    current = repo_root
    evidence: list[tuple[str, tuple[int, int, int, int, int]]] = []
    for part in PurePosixPath(relative_path).parts[:-1]:
        current = current / part
        value = _lstat_for_read(current)
        if value is None or not stat_module.S_ISDIR(value.st_mode) or _is_redirect(current):
            raise UnsafePathError("candidate has a missing or redirected parent component")
        signature = _stat_signature(value)
        assert signature is not None
        evidence.append((part, signature))
    return tuple(evidence)


def _verify_parent_components(
    repo_root: Path,
    relative_path: str,
    expected: tuple[tuple[str, tuple[int, int, int, int, int]], ...],
) -> None:
    try:
        current = _validated_parent_components(repo_root, relative_path)
    except UnsafePathError as exc:
        raise RepositoryChangedError("candidate parent was redirected during read") from exc
    if current != expected:
        raise RepositoryChangedError("candidate parent identity changed during read")


def _policy_candidates(repo_root: Path, policy: ScanPolicy) -> list[Candidate]:
    discovered: dict[str, Candidate] = {}
    for raw_path in policy.discovered_metadata_paths:
        relative_path = normalize_relative_path(raw_path)
        if _has_redirected_parent(repo_root, relative_path):
            continue
        path = repo_root / Path(*PurePosixPath(relative_path).parts)
        value = _safe_lstat(path)
        if value is not None and not stat_module.S_ISDIR(value.st_mode):
            discovered[path_key(relative_path)] = Candidate(relative_path, "POLICY_DISCOVERED")
    for raw_path in policy.discovered_pruned_roots:
        relative_path = normalize_relative_path(raw_path)
        if _has_redirected_parent(repo_root, relative_path):
            continue
        path = repo_root / Path(*PurePosixPath(relative_path).parts)
        value = _safe_lstat(path)
        if value is not None and (stat_module.S_ISDIR(value.st_mode) or _is_redirect(path)):
            discovered[path_key(relative_path)] = Candidate(relative_path, "POLICY_DISCOVERED")
    return [discovered[key] for key in sorted(discovered)]


def verify_scan_evidence(repo_root: Path, facts: ScanFacts, policy: ScanPolicy) -> bool:
    for evidence in facts.evidence.values():
        try:
            matches, _ = compare_persisted_evidence(
                repo_root,
                evidence,
                policy,
                content_semantics=False,
            )
        except RepositoryChangedError, UnsafePathError:
            return False
        if not matches:
            return False
    return True


def compare_persisted_evidence(
    repo_root: Path,
    evidence: ObjectEvidence,
    policy: ScanPolicy,
    *,
    content_semantics: bool,
) -> tuple[bool, str]:
    """Compare one persisted proof through the scanner-owned safe observation boundary.

    Publication validation uses exact object evidence. Strong currentness deliberately
    treats safe text content as the semantic proof and therefore does not make an
    mtime-only change stale. Hard-secret content is never opened by this operation.
    """

    relative_path = evidence.relative_path
    path = repo_root / Path(*PurePosixPath(relative_path).parts)
    redirected = _has_redirected_component(repo_root, relative_path)
    if evidence.evidence_kind in {"REDIRECT", "REDIRECTED_PRUNED_ROOT"}:
        return redirected, "redirect_unchanged" if redirected else "redirect_removed"
    if redirected:
        return False, "path_became_redirected"

    current = _safe_lstat(path)
    if evidence.evidence_kind == "MISSING":
        return current is None, "missing_unchanged" if current is None else "path_appeared"
    if current is None:
        return False, "path_missing"

    current_signature = _stat_signature(current)
    if evidence.evidence_kind == "HARD_SECRET":
        if hard_secret_reason(relative_path) is None:
            return False, "hard_secret_classification_changed"
        if content_semantics:
            return True, "hard_secret_content_excluded"
        return (
            current_signature == evidence.stat_signature,
            "hard_secret_metadata_unchanged"
            if current_signature == evidence.stat_signature
            else "hard_secret_metadata_changed",
        )

    if evidence.evidence_kind == "TEXT":
        if not stat_module.S_ISREG(current.st_mode):
            return False, "text_path_not_regular"
        if not supported_text(relative_path):
            return False, "text_classification_changed"
        if current.st_size > policy.max_text_bytes:
            return False, "text_size_limit_exceeded"
        if not content_semantics and current_signature != evidence.stat_signature:
            return False, "object_metadata_changed"
        raw, binary = _read_safe_file(
            repo_root,
            path,
            current,
            policy,
            preclassified_safe=True,
        )
        if binary or raw is None:
            return False, "text_became_binary"
        matches = hashlib.sha256(raw).hexdigest() == evidence.content_hash
        return matches, "content_unchanged" if matches else "content_hash_changed"

    if current_signature != evidence.stat_signature:
        return False, "bounded_metadata_changed"
    if evidence.evidence_kind in {"BINARY", "UNSUPPORTED_ENCODING"} and content_semantics:
        if not stat_module.S_ISREG(current.st_mode):
            return False, "bounded_object_not_regular"
        raw, binary = _read_safe_file(
            repo_root,
            path,
            current,
            policy,
            preclassified_safe=True,
        )
        if evidence.evidence_kind == "BINARY":
            return binary, "binary_classification_unchanged" if binary else "binary_became_text"
        if binary or raw is None:
            return False, "encoding_object_became_binary"
        try:
            raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
        except UnicodeDecodeError:
            return True, "encoding_classification_unchanged"
        return False, "unsupported_encoding_became_text"
    return True, "bounded_metadata_unchanged"


def _read_safe_file(
    repo_root: Path,
    path: Path,
    expected_stat: os.stat_result,
    policy: ScanPolicy,
    *,
    preclassified_safe: bool = False,
) -> tuple[bytes | None, bool]:
    lexical_root = repo_root.absolute()
    lexical_path = path.absolute()
    if not lexical_path.is_relative_to(lexical_root):
        raise UnsafePathError("unsafe file path")
    if _is_redirect(path):
        if preclassified_safe:
            raise RepositoryChangedError("candidate became a redirect after classification")
        raise UnsafePathError("unsafe file path")
    if not stat_module.S_ISREG(expected_stat.st_mode):
        raise UnsafePathError("candidate is not a regular file")
    relative_path = lexical_path.relative_to(lexical_root).as_posix()
    try:
        parent_evidence = _validated_parent_components(repo_root, relative_path)
    except UnsafePathError as exc:
        if preclassified_safe:
            raise RepositoryChangedError(
                "candidate parent became unsafe after classification"
            ) from exc
        raise
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RepositoryChangedError("candidate parent disappeared before open") from exc
    except PermissionError as exc:
        raise FileAccessError("candidate parent could not be inspected") from exc
    except OSError as exc:
        raise FileAccessError("candidate parent could not be resolved") from exc
    if not resolved_parent.is_relative_to(repo_root.resolve(strict=True)):
        raise RepositoryChangedError("candidate parent was redirected outside repository")

    # Python exposes O_NOFOLLOW for the final component on supporting platforms.
    # Windows has no equivalent kernel-level flag here, so parent reparse points and
    # object identities are checked immediately before and after the bounded read.
    _verify_parent_components(repo_root, relative_path, parent_evidence)
    current_before_open = _lstat_for_read(path)
    if (
        current_before_open is None
        or not _same_identity(expected_stat, current_before_open)
        or _is_redirect(path)
    ):
        raise RepositoryChangedError("candidate identity changed immediately before open")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        current_after_error = _lstat_for_read(path)
        if (
            current_after_error is None
            or _is_redirect(path)
            or not _same_identity(expected_stat, current_after_error)
        ):
            raise RepositoryChangedError("candidate changed before open completed") from exc
        raise FileAccessError("candidate could not be opened for bounded reading") from exc
    try:
        try:
            opened_before = os.fstat(descriptor)
        except OSError as exc:
            raise FileReadError("opened candidate could not be inspected") from exc
        if not _same_identity(expected_stat, opened_before) or _is_redirect(path):
            raise RepositoryChangedError("candidate identity changed between lstat and open")
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                data, binary = _read_bounded_stream(
                    handle,
                    max_bytes=policy.max_text_bytes,
                    probe_bytes=policy.binary_probe_bytes,
                )
                opened_after = os.fstat(handle.fileno())
        except RepositoryChangedError:
            raise
        except OSError as exc:
            raise FileReadError("candidate content could not be read") from exc
        current = _lstat_for_read(path)
        _verify_parent_components(repo_root, relative_path, parent_evidence)
        if (
            not _same_identity(opened_before, opened_after)
            or current is None
            or not _same_identity(expected_stat, current)
            or _is_redirect(path)
        ):
            raise RepositoryChangedError("candidate identity changed during read")
        return data, binary
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _read_bounded_stream(
    handle: BinaryIO,
    *,
    max_bytes: int,
    probe_bytes: int,
) -> tuple[bytes | None, bool]:
    probe_limit = min(max_bytes + 1, max(1, probe_bytes))
    probe = handle.read(probe_limit)
    if _looks_binary(probe):
        return None, True
    remaining = handle.read(max_bytes + 1 - len(probe))
    data = probe + remaining
    if len(data) > max_bytes:
        raise RepositoryChangedError("file changed beyond configured text limit during read")
    return data, False


def _lstat_for_read(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FileAccessError("candidate metadata could not be inspected") from exc


def _looks_binary(probe: bytes) -> bool:
    if b"\0" in probe:
        return True
    if not probe:
        return False
    controls = sum(byte < 9 or 13 < byte < 32 for byte in probe)
    return controls / len(probe) > 0.30


def _stat_signature(value: os.stat_result | None) -> tuple[int, int, int, int, int] | None:
    if value is None:
        return None
    return (value.st_mode, value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino)


def _same_identity(expected: os.stat_result, actual: os.stat_result) -> bool:
    if not stat_module.S_ISREG(actual.st_mode):
        return False
    if expected.st_dev and actual.st_dev and expected.st_dev != actual.st_dev:
        return False
    if expected.st_ino and actual.st_ino and expected.st_ino != actual.st_ino:
        return False
    return (
        expected.st_mode == actual.st_mode
        and expected.st_size == actual.st_size
        and expected.st_mtime_ns == actual.st_mtime_ns
    )


def _line_count(source: str) -> int:
    if not source:
        return 0
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def _git_z(
    repo_root: Path,
    *args: str,
    index_view: TemporaryGitIndex | None = None,
) -> list[str]:
    try:
        return git_z(repo_root, *args, index_view=index_view)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"Git repository inspection failed: {type(exc).__name__}") from exc


def _git_text(
    repo_root: Path,
    *args: str,
    allow_failure: bool = False,
    index_view: TemporaryGitIndex | None = None,
) -> str:
    try:
        return git_text(
            repo_root,
            *args,
            allow_failure=allow_failure,
            index_view=index_view,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"Git repository inspection failed: {type(exc).__name__}") from exc


def _git_bytes(
    repo_root: Path,
    *args: str,
    index_view: TemporaryGitIndex | None = None,
) -> bytes:
    try:
        return git_bytes(repo_root, *args, index_view=index_view)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"Git repository inspection failed: {type(exc).__name__}") from exc
