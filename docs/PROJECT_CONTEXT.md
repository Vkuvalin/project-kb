# Project Context

Project KB is a local project knowledge layer for agent-assisted development.

The current implementation provides the project foundation, registry/storage
core, project preflight resolver, and deterministic structural index:

- Python package layout
- Typer CLI entrypoint
- JSON response envelope
- basic error model and exit codes
- pytest and Ruff development baseline
- Project KB home resolution
- `registry.sqlite` initialization
- project registration, listing, relinking, and unregistering
- per-project storage directories for snapshots, future exports, and run records
- explicit-name and current-directory project resolution
- repository path, Git-root, and lightweight identity checks
- exact Project KB storage-layout diagnostics without silent repair
- structured project states, recommended actions, availability flags, and
  command-gating primitives
- an additive registry schema migration for hashed repository fingerprint data
- full Git-populated rebuilds, plus bounded universal technical prune-root
  visibility, into an atomically published per-project `kb.sqlite`
- deterministic file classification, bounded text decoding/hashing, Python AST
  symbols and imports, exact basic relations, and parse diagnostics
- exact structural CLI queries by file and symbol name
- snapshot status that distinguishes no snapshot, valid-but-currentness-
  unverified, registry reconciliation warnings, last-index-failed with previous
  snapshot, semantic rebuild requirements, and storage errors
- final candidate/object evidence verification after temporary SQLite
  validation, with one repository-change retry
- separate repository-mutation, unsafe-path, file-access, and file-read failure
  semantics
- sealed temporary snapshots, one-step os.replace publication without canonical
  SQLite mutation, and an exact snapshot-to-index-run relationship
- factual recorded/not_recorded/unknown registry and run-file bookkeeping
- schema-manifest, cross-table, and scanner/policy/extractor compatibility
  validation with exact query provenance

The current implementation does not include incremental indexing, generic
source chunks, full source-text storage, semantic search, embeddings, call or
reference graphs, exports, context packs, hooks, MCP integration, web/API
surfaces, or LLM/provider calls.

V0 has no reference-repository policy defaults and no public per-project scanner
policy. Its ignored-path visibility is limited to universal technical roots, so
non-standard ignored runtime files or directories—and commonly ignored .env
files—may be absent. Hard-secret handling applies whenever such a path enters
candidate population. Environment templates remain indexable. Windows path
safety uses fail-closed parent/final-component reparse and identity checks around
the bounded read; Python does not expose a kernel-level no-follow guarantee for
all Windows path components.
