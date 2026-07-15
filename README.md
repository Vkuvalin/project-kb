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
pkb status my-project --verify strong --json
pkb symbols my-project --file src/package/module.py --json
pkb symbols my-project --name MyClass --json
pkb imports my-project --file src/package/module.py --json
pkb inspect my-project --file src/package/module.py --json
```

Omit the project name when the current directory is inside a registered Git
repository. `pkb index` and `pkb index --full` both perform the same full rebuild;
incremental refresh is not implemented.

Status keeps snapshot availability separate from currentness. Without
`--verify`, a valid semantic-v2 snapshot reports `currentness = UNVERIFIED` and
`truth_claim = CAPTURED_STABLE`: it was built, sealed, repository-bound, and
atomically published from a stable observation, without claiming that the
workspace cannot change afterward. `--verify strong` is the only authoritative
existing-snapshot verifier. It rebuilds an isolated temporary index under
managed project storage from a stable staged-entry view without
assume-unchanged or skip-worktree bits, then observes candidates/status through
that visibility-neutral view. The live raw-index identity plus logical
staged-entry and visibility projections are sealed before/after;
stable visibility flags are rejected and any detected index instability retries
rather than producing `CURRENT`. The real Git index is never modified.
Strong verification checks the persisted v2 candidate and per-object proof manifest;
stable whole-repository Git status is diagnostic, not an independent stale
predicate outside that declared scope. It reseals live repository identity
before completion and reports `CURRENT`, `STALE`, `UNVERIFIED`,
`CHANGED_DURING_CHECK`, or `ERROR`. A `CURRENT` result carries
`truth_claim = CURRENT_AT_VERIFIED_TIME`, `verification_mode = strong`, and an
absolute UTC `verified_at`; it does not claim permanent currentness. The legacy
`--verify fast` token returns `FAST_VERIFICATION_REMOVED`, recommends strong
verification or a new full capture, and performs no proof or state mutation.
It emits no verification timestamp or timing. Verification is manual; there is
no watcher or automatic refresh. Valid legacy v1 snapshots remain readable,
but strong currentness verification returns an explicit full-reindex action
without fabricating verification time. Structural SQLite queries against an
available snapshot remain bounded lookups; they are distinct from currentness
verification and future incremental refresh.
Every explicit relink rotates the active registry binding generation, so an
older snapshot remains unavailable—even after a failed reindex—until a full
reindex succeeds for that binding.

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
bookkeeping results are reported as recorded, not_recorded, or unknown. New
snapshots use semantic schema v2 with active-repository binding, canonical
module/occurrence identity, and an explicit verification proof manifest.
Root packaging evidence is classified as absent, supported, unsupported, or
unreadable. Convention-based exact module identity is allowed only when that
evidence is absent; `setup.cfg`, `setup.py`, unsupported backends, and
unreadable `pyproject.toml` remain explicitly non-exact. Explicit roots and
supported unambiguous packaging roots retain precedence, and Project KB never
executes packaging files. A fail-closed non-exact module row is a valid semantic
v2 state rather than snapshot corruption, so the snapshot may still be sealed
and published. Canonical module names and logical symbol keys remain unavailable
for those files until the source-root authority is statically supported or
explicitly supplied.
Relinking preserves project storage but requires a full reindex before the old
snapshot can be exposed for the new repository binding.

## Development

```powershell
uv sync
uv run pkb --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
