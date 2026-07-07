# Development Checklist

Before handing off code changes:

- Run `uv sync` after dependency changes.
- Run `uv run pytest`.
- Run `uv run ruff check .`.
- Run `uv run ruff format --check .`.
- Keep tests isolated from real user data by setting `PROJECT_KB_HOME` to a test
  temporary directory.

Do not add real registry, resolver, scanner, indexer, search, export, hook, MCP,
embedding, or LLM behavior during Stage 1.
