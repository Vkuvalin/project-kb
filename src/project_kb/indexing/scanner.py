"""Git-populated, bounded, read-only repository scanner."""

import contextlib
import hashlib
import os
import stat as stat_module
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from project_kb.indexing.extractor import extract_python, stable_id
from project_kb.indexing.models import (
    Candidate,
    FileFact,
    ImportFact,
    ObjectEvidence,
    PrunedRootFact,
    RelationFact,
    RepoState,
    ScanFacts,
    ScanPolicy,
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


def git_candidates(repo_root: Path, policy: ScanPolicy | None = None) -> list[Candidate]:
    tracked = _git_z(repo_root, "ls-files", "-z")
    untracked = _git_z(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
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
    candidates = candidates if candidates is not None else git_candidates(repo_root, policy)
    head = _git_text(repo_root, "rev-parse", "--verify", "HEAD", allow_failure=True) or None
    branch = (
        _git_text(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
        or None
    )
    status = _git_bytes(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    candidate_payload = "".join(f"{c.population}\0{c.relative_path}\0" for c in candidates)
    return RepoState(
        head=head,
        branch=branch,
        status_fingerprint=hashlib.sha256(status).hexdigest(),
        candidate_fingerprint=hashlib.sha256(candidate_payload.encode("utf-8")).hexdigest(),
    )


def scan_repository(repo_root: Path, candidates: list[Candidate], policy: ScanPolicy) -> ScanFacts:
    facts = ScanFacts(candidate_count=len(candidates))
    classification_ns = 0
    read_ns = 0
    parse_ns = 0
    pruned_seen: set[str] = set()

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
        if file_fact.language == "python":
            parse_start = time.perf_counter_ns()
            symbols, imports, relations, diagnostics = extract_python(
                file_id=file_id,
                relative_path=relative_path,
                source=source,
                line_count=line_count,
            )
            parse_ns += time.perf_counter_ns() - parse_start
            file_fact.parse_status = "FAILED" if diagnostics else "SUCCESS"
            facts.symbols.extend(symbols)
            facts.imports.extend(imports)
            facts.relations.extend(relations)
            facts.diagnostics.extend(diagnostics)

    relations_start = time.perf_counter_ns()
    _resolve_imports(facts)
    facts.timings.update(
        {
            "classification_ms": classification_ns // 1_000_000,
            "read_hash_ms": read_ns // 1_000_000,
            "parse_ms": parse_ns // 1_000_000,
            "relations_ms": (time.perf_counter_ns() - relations_start) // 1_000_000,
        }
    )
    return facts


def _resolve_imports(facts: ScanFacts) -> None:
    file_by_id = {file.file_id: file for file in facts.files}
    module_map: dict[str, set[str]] = {}
    local_roots: set[str] = set()
    for file in facts.files:
        if file.language != "python":
            continue
        for module in _module_names(file.relative_path):
            module_map.setdefault(module, set()).add(file.file_id)
            if module:
                local_roots.add(module.split(".", 1)[0])

    for item in facts.imports:
        source = file_by_id[item.file_id]
        target_module = _absolute_import_target(source.relative_path, item)
        possible = [target_module]
        if item.import_kind == "IMPORT_FROM" and item.imported_name not in {None, "*"}:
            possible.insert(0, f"{target_module}.{item.imported_name}".strip("."))
        matches: set[str] = set()
        for module in possible:
            matches = module_map.get(module, set())
            if matches:
                break
        if len(matches) == 1:
            item.resolution_status = "EXACT"
            item.resolved_file_id = next(iter(matches))
        elif len(matches) > 1:
            item.resolution_status = "AMBIGUOUS"
        elif item.relative_level or target_module.split(".", 1)[0] in local_roots:
            item.resolution_status = "UNRESOLVED"
        else:
            item.resolution_status = "EXTERNAL"

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
        relative_path = evidence.relative_path
        path = repo_root / Path(*PurePosixPath(relative_path).parts)
        redirected = _has_redirected_component(repo_root, relative_path)
        if evidence.evidence_kind in {"REDIRECT", "REDIRECTED_PRUNED_ROOT"}:
            if not redirected:
                return False
            continue
        if redirected:
            return False
        current = _safe_lstat(path)
        if _stat_signature(current) != evidence.stat_signature:
            return False
        if evidence.evidence_kind == "TEXT":
            if current is None:
                return False
            try:
                raw, binary = _read_safe_file(
                    repo_root,
                    path,
                    current,
                    policy,
                    preclassified_safe=True,
                )
            except RepositoryChangedError, UnsafePathError:
                return False
            if binary or raw is None or hashlib.sha256(raw).hexdigest() != evidence.content_hash:
                return False
    return True


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


def _git_z(repo_root: Path, *args: str) -> list[str]:
    payload = _git_bytes(repo_root, *args)
    return [item.decode("utf-8", errors="surrogateescape") for item in payload.split(b"\0") if item]


def _git_text(repo_root: Path, *args: str, allow_failure: bool = False) -> str:
    process = _run_git(repo_root, *args, check=not allow_failure)
    return (
        process.stdout.decode("utf-8", errors="strict").strip() if process.returncode == 0 else ""
    )


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    return _run_git(repo_root, *args, check=True).stdout


def _run_git(repo_root: Path, *args: str, check: bool) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=repo_root,
            check=check,
            capture_output=True,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError(f"Git repository inspection failed: {type(exc).__name__}") from exc
