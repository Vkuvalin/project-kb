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
- additive registry schema migrations for hashed repository fingerprints and
  active/snapshot binding generations, using durable relink/index-outcome
  ordering and failing closed when legacy history cannot prove alignment
- full Git-populated rebuilds, plus bounded universal technical prune-root
  visibility, into an atomically published per-project `kb.sqlite`
- deterministic file classification, bounded text decoding/hashing, Python AST
  symbols and imports, exact basic relations, and parse diagnostics
- one canonical source-root/module mapper plus logical symbol keys and
  snapshot-bound file/symbol occurrence identities; root packaging evidence is
  absent, supported, unsupported, or unreadable, and convention remains
  available only for provably absent evidence
- exact structural CLI queries by file and symbol name
- snapshot status that distinguishes no snapshot, valid-but-currentness-
  unverified, registry reconciliation warnings, last-index-failed with previous
  snapshot, semantic rebuild requirements, and storage errors
- strong-only authoritative v2 currentness verification with distinct `CURRENT`,
  `STALE`, `UNVERIFIED`, `CHANGED_DURING_CHECK`, and `ERROR` outcomes;
  `CURRENT` means `CURRENT_AT_VERIFIED_TIME` and carries its UTC completion time
- one visibility-neutral temporary-index observation authority for strong
  checks, sealed by live-index generation evidence without modifying the real
  index, plus live repository-identity sealing; strong semantic stale decisions
  use scoped candidate/object proof,
  with raw Git status retained for instability detection and diagnostics
- final candidate/object evidence verification after temporary SQLite
  validation, with one repository-change retry
- separate repository-mutation, unsafe-path, file-access, and file-read failure
  semantics
- sealed temporary snapshots, one-step os.replace publication without canonical
  SQLite mutation, an exact snapshot-to-index-run relationship, and
  generation-aware post-publication classification that cannot report a
  physically published but relink-quarantined snapshot as active success
- factual recorded/not_recorded/unknown registry and run-file bookkeeping
- schema-manifest, cross-table, and scanner/policy/extractor compatibility
  validation with exact query provenance
- version-first v1/v2 classification, active repository binding, and v2 proof,
  module-map, verifier, and occurrence-contract validation
- one bounded Git runner for scanner, resolver, repository identity, status,
  and currentness observations, with exact argument profiles, no inherited
  `GIT_*` controls, local `core.fsmonitor` disabled, and only managed-storage
  temporary-index construction from staged entries permitted as a write-shaped
  Git operation

The current implementation does not include incremental indexing, generic
source chunks, full source-text storage, semantic search, embeddings, call or
reference graphs, exports, context packs, hooks, MCP integration, web/API
surfaces, or LLM/provider calls.

Currentness verification is explicit and post-publication. There are no
watchers, automatic refreshes, incremental updates, or automatic v1/v2
downgrades. Every relink rotates a registry binding generation; failed reindex
bookkeeping cannot make the prior generation's snapshot available again.
Normal semantic-v2 status is `UNVERIFIED` with `CAPTURED_STABLE`. Strong
verification of valid v1 remains unexecuted and `UNVERIFIED`, with an explicit
full-v2-reindex action and no fabricated duration or verification timestamp.
The legacy fast token returns `FAST_VERIFICATION_REMOVED` before snapshot or
repository inspection and recommends strong verification or a new full capture.

V0 has no reference-repository policy defaults and no public per-project scanner
policy. Its ignored-path visibility is limited to universal technical roots, so
non-standard ignored runtime files or directories—and commonly ignored .env
files—may be absent. Hard-secret handling applies whenever such a path enters
candidate population. Environment templates remain indexable. Windows path
safety uses fail-closed parent/final-component reparse and identity checks around
the bounded read; Python does not expose a kernel-level no-follow guarantee for
all Windows path components.
