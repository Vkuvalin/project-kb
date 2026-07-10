# Development Checklist

Before handing off code changes:

- Run `uv sync` after dependency changes.
- Run `uv run pkb --help`.
- For registry/resolver/storage changes, use an explicit temporary
  `PROJECT_KB_HOME`, register only a safe test repository, and run both
  `uv run pkb status <project_name> --json` and `uv run pkb status --json`.
- Never run destructive, mismatch, missing-path, or storage-removal validation
  against a real registration or real Project KB home.
- Run `uv run pytest`.
- Run `uv run ruff check .`.
- Run `uv run ruff format --check .`.
- Run `git diff --check`.
- Keep tests isolated from real user data by setting `PROJECT_KB_HOME` to a test
  temporary directory.
- Keep Git fingerprint tests local and deterministic. Do not fetch or probe
  remotes, and assert that raw origin URLs never enter output or registry data.

The current implementation does not include project `kb.sqlite`, file/chunk
scanning, snapshots, indexing, staleness detection, search, exports, context
packs, hooks, MCP, embeddings, or LLM/provider behavior.
