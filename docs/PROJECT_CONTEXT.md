# Project Context

Project KB is a local project knowledge layer for agent-assisted development.

The current implementation provides the project foundation, registry/storage
core, and a project preflight resolver:

- Python package layout
- Typer CLI entrypoint
- JSON response envelope
- basic error model and exit codes
- pytest and Ruff development baseline
- Project KB home resolution
- `registry.sqlite` initialization
- project registration, listing, relinking, and unregistering
- per-project storage directories for future exports and runs
- explicit-name and current-directory project resolution
- repository path, Git-root, and lightweight identity checks
- exact Project KB storage-layout diagnostics without silent repair
- structured project states, recommended actions, availability flags, and
  command-gating primitives
- an additive registry schema migration for hashed repository fingerprint data
- a future-compatible snapshot placeholder; valid projects currently return
  `REGISTERED_NO_SNAPSHOT`

The current implementation does not include project databases, file/chunk
scanning, snapshot creation, indexing, staleness detection, search, exports,
context packs, hooks, MCP integration, embeddings, or LLM/provider calls.
