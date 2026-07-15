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
  temporary directory under managed project storage.
- Keep Git fingerprint tests local and deterministic. Do not fetch or probe
  remotes, and assert that raw origin URLs never enter output or registry data.
- For any Git observation change, use the shared allow-listed runner and assert
  `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL`, `GIT_NO_LAZY_FETCH`, and
  `GIT_OPTIONAL_LOCKS=0`; cover removal of inherited repository/index/object
  redirectors and dynamic `GIT_CONFIG_*` injection, exact argument rejection,
  disabled local `core.fsmonitor` execution, controlled managed-storage temporary-only
  `GIT_INDEX_FILE`, cleanup, and byte-for-byte preservation of the live index.
- For indexer changes, cover tracked plus untracked/non-ignored Git population,
  universal-only bounded ignored-root visibility, Git path-spelling priority,
  absence of reference-derived defaults, hard-secret no-read/no-hash behavior,
  exact credential/token basenames, safe environment templates, ignored .env
  absence, ordinary credential/token-named source, probe-first binary handling,
  large files, strict UTF-8/BOM decoding, opened-file identity, and
  parent/final-component redirect races.
- Validate Python syntax-failure recovery, deterministic import resolution,
  repeated full-rebuild logical equivalence, repository-change detection, and
  previous-snapshot preservation after build/publication failure.
- Validate final candidate/evidence verification after temporary build and
  validation, one retry followed by REPO_CHANGED_DURING_SCAN, and refusal to
  publish when the repository changes immediately before replacement.
- Distinguish proven mutation from static unsafe paths, permissions, sharing
  failures, ordinary open failures, and ordinary read failures; only mutation
  receives the bounded retry.
- Validate schema manifests and compatibility mismatches separately from
  physical SQLite corruption. Check snapshot_meta.run_id against exactly one
  matching run and validate SEALED/BUILD_SUCCEEDED before publication. Assert
  that os.replace is the sole commit and canonical SQLite bytes are not mutated
  afterward.
- Distinguish valid v1, valid v2, unknown/incompatible, corrupt, and wrong-
  repository snapshots before applying version-specific invariants. Assert that
  v1-to-v2 cutover is a full source reindex, never row migration.
- Cover canonical source-root mapping, ambiguous roots, scripts, namespace
  layouts, duplicate logical definitions, snapshot-bound occurrence IDs,
  unsupported or unreadable packaging that must not fall through to convention,
  safe `setup.cfg`/`setup.py` marker detection without execution, supported
  packaging, no-packaging convention, and explicit-root precedence.
- For currentness changes, cover explicit `FAST_VERIFICATION_REMOVED`, no fast
  `CURRENT`/`STALE`, timestamp, timing, or state mutation, the full strong
  mutation matrix, same-status dirty changes, bounded retry,
  `CHANGED_DURING_CHECK`, verifier `ERROR`, proof corruption, hard-secret
  exclusions, unconditional terminal candidate/state/binding/object proof,
  clean-commit races, stable assume-unchanged and skip-worktree state, and an
  enable/hidden-write/disable ABA inside the final authoritative capture for
  both flags under strong verification. Exercise the staged-entry-derived,
  visibility-neutral temporary-index
  production boundary, live-index generation retry, and no-real-index-mutation;
  also cover live identity
  mismatch/change/change-back under strong verification,
  relink-generation fail-closed across failed reindex, UTC `verified_at` on
  completed strong outcomes, and no fabricated fast or v1 timing.
- For strong scope, prove staging-only transitions with unchanged worktree
  bytes, tracked hard-secret changes, and tracked pruned-root changes remain
  `CURRENT` only within explicit exclusions. Raw status instability must retry;
  included content and relevant candidate additions/removals remain `STALE`.
- For legacy registry migration, use realistic historical relink/index-outcome
  events, including same-root relink, failed/later-successful reindex, missing
  history, idempotence, and bounded valid-v1 readability.
- Check registry and run-file bookkeeping independently for recorded,
  not_recorded, and unknown outcomes. Later ancillary failure must preserve
  already proven outcomes when the active binding is unchanged. A same-root
  relink after replace but before outcome recording must quarantine the new
  snapshot and must not be reported as usable success.
- Verify missing-file query errors, empty facts for an indexed file, provenance,
  binary-collated case-sensitive literal symbol prefixes (including underscore,
  percent, and empty input), and warning propagation when a previous snapshot
  is queried after a failed refresh.
- Dogfood only with an isolated temporary `PROJECT_KB_HOME`. Capture the target
  repository's HEAD, branch, and full porcelain status before and after; never
  execute target modules, scripts, tests, hooks, migrations, or services.
- Check `pkb symbols`, `pkb imports`, and `pkb inspect` against bounded exact
  path/line samples after a successful rebuild.

The current implementation is full-rebuild with explicit post-publication
strong verification; public affirmative fast verification is removed. Do not
claim incremental or automatic refresh,
watchers, unconditional currentness without verification, generic chunk/search
behavior, runtime dependency tracing, cross-snapshot lineage, or semantic graph
coverage.
