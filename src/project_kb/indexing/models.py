"""Typed in-memory facts used while building a structural snapshot."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScanPolicy:
    max_text_bytes: int = 2 * 1024 * 1024
    binary_probe_bytes: int = 8192
    policy_version: str = "stage4-v0-3"
    discovered_metadata_paths: tuple[str, ...] = ()
    discovered_pruned_roots: tuple[str, ...] = (
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        "__pycache__",
    )


@dataclass(frozen=True)
class RepoState:
    head: str | None
    branch: str | None
    status_fingerprint: str
    candidate_fingerprint: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class Candidate:
    relative_path: str
    population: str


@dataclass
class FileFact:
    file_id: str
    relative_path: str
    path_key: str
    git_population: str
    file_kind: str
    language: str | None
    extension: str
    size_bytes: int | None
    mtime_ns: int | None
    line_count: int | None
    encoding: str | None
    content_hash: str | None
    analysis_level: str
    classification_reason: str
    parse_status: str


@dataclass
class PrunedRootFact:
    relative_path: str
    category: str
    reason: str
    source_policy: str


@dataclass
class SymbolFact:
    symbol_id: str
    file_id: str
    qualified_name: str
    short_name: str
    symbol_kind: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int | None
    parent_symbol_id: str | None
    signature_text: str | None


@dataclass
class ImportFact:
    import_id: str
    file_id: str
    import_kind: str
    module_text: str
    imported_name: str | None
    alias: str | None
    relative_level: int
    start_line: int
    end_line: int
    resolution_status: str = "UNRESOLVED"
    resolved_file_id: str | None = None


@dataclass
class RelationFact:
    relation_id: str
    relation_kind: str
    source_file_id: str | None
    source_symbol_id: str | None
    target_file_id: str | None
    target_symbol_id: str | None
    target_text: str | None
    start_line: int | None
    end_line: int | None
    start_column: int | None
    end_column: int | None
    resolution_status: str
    evidence_kind: str


@dataclass
class DiagnosticFact:
    file_id: str
    diagnostic_kind: str
    message: str
    line: int | None
    column: int | None


@dataclass(frozen=True)
class ObjectEvidence:
    relative_path: str
    evidence_kind: str
    stat_signature: tuple[int, int, int, int, int] | None
    content_hash: str | None = None


@dataclass
class ScanFacts:
    files: list[FileFact] = field(default_factory=list)
    pruned_roots: list[PrunedRootFact] = field(default_factory=list)
    symbols: list[SymbolFact] = field(default_factory=list)
    imports: list[ImportFact] = field(default_factory=list)
    relations: list[RelationFact] = field(default_factory=list)
    diagnostics: list[DiagnosticFact] = field(default_factory=list)
    evidence: dict[str, ObjectEvidence] = field(default_factory=dict)
    candidate_count: int = 0
    candidate_bytes: int = 0
    timings: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "candidates": self.candidate_count,
            "candidate_bytes": self.candidate_bytes,
            "text_files": sum(f.analysis_level == "TEXT_STRUCTURAL" for f in self.files),
            "metadata_only": sum(f.analysis_level == "METADATA_ONLY" for f in self.files),
            "pruned_roots": len(self.pruned_roots),
            "parsed_files": sum(f.parse_status == "SUCCESS" for f in self.files),
            "parse_failures": sum(f.parse_status == "FAILED" for f in self.files),
            "symbols": len(self.symbols),
            "imports": len(self.imports),
            "relations": len(self.relations),
        }


@dataclass(frozen=True)
class IndexOutcome:
    result: str
    code: str
    message: str
    data: dict[str, Any]
    warnings: list[dict[str, Any]]
