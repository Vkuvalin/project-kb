# Project KB

Local project knowledge layer for agent-assisted development.

Project KB registers local Git repositories and builds deterministic structural
snapshots outside the source repository. The current index contains safe file
metadata and hashes, Python AST symbols/imports, basic exact relations, and
parse diagnostics. It never imports or executes target code.

Candidate population is Git tracked plus untracked/non-ignored files. V0 also
performs bounded exact checks for a small universal set of technical prune roots
such as .venv, node_modules, and __pycache__; it does not recursively crawl
ignored trees. There are no reference-repository defaults or public per-project
scanner policies. Non-standard ignored runtime paths may therefore be absent
from the snapshot.

## Usage

```powershell
pkb register my-project C:\path\to\repository --json
pkb index my-project --full --json
pkb status my-project --json
pkb symbols my-project --file src/package/module.py --json
pkb symbols my-project --name MyClass --json
pkb imports my-project --file src/package/module.py --json
pkb inspect my-project --file src/package/module.py --json
```

Omit the project name when the current directory is inside a registered Git
repository. `pkb index` and `pkb index --full` both perform the same full rebuild;
incremental refresh is not implemented.

File-scoped `symbols` and `imports` queries return `FILE_NOT_INDEXED` when the
path is absent. Queries against a preserved snapshot after a failed refresh
succeed with an explicit warning and include snapshot, file-hash, and extractor
provenance. The --name filter is a case-sensitive literal prefix; an empty
prefix matches all indexed symbols.

When they enter Git-based candidate population, real environment files such as
.env, .env.local, and .env.production and exact credential/token basenames are
metadata-only hard secrets and are never opened or content-hashed. An ignored
.env commonly remains absent because V0 does not enumerate ignored files.
Template files .env.example, .env.sample, .env.template, and .env.dist remain
eligible for structural text indexing.

Snapshot databases are fully built and sealed before publication. One
same-volume os.replace is the only canonical publication commit; the canonical
SQLite file is not reopened for state mutation. Registry and run-file
bookkeeping results are reported as recorded, not_recorded, or unknown.

## Development

```powershell
uv sync
uv run pkb --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
