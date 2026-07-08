# Project Context

Project KB is a local project knowledge layer for agent-assisted development.

Stage 2 provides the project foundation skeleton plus registry and storage core:

- Python package layout
- Typer CLI entrypoint
- JSON response envelope
- basic error model and exit codes
- pytest and Ruff development baseline
- Project KB home resolution
- `registry.sqlite` initialization
- project registration, listing, relinking, and unregistering
- per-project storage directories for future exports and runs

Stage 2 does not include project databases, full project state resolution,
scanning, indexing, search, exports, context packs, hooks, MCP integration,
embeddings, or LLM calls.
