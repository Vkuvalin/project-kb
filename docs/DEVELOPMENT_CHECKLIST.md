# Development Checklist

Before handing off code changes:

- Run `uv sync` after dependency changes.
- Run `uv run pkb --help`.
- For registry/storage changes, run `uv run pkb register project-kb . --json`
  and `uv run pkb projects --json` with an appropriate Project KB home.
- Run `uv run pytest`.
- Run `uv run ruff check .`.
- Run `uv run ruff format --check .`.
- Keep tests isolated from real user data by setting `PROJECT_KB_HOME` to a test
  temporary directory.

Do not add project `kb.sqlite`, resolver, scanner, indexer, search, export, hook,
MCP, embedding, or LLM behavior during Stage 2.
