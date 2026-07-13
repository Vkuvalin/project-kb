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
- Check registry and run-file bookkeeping independently for recorded,
  not_recorded, and unknown outcomes. Later ancillary failure must preserve
  already proven outcomes and must never invalidate the published snapshot.
- Verify missing-file query errors, empty facts for an indexed file, provenance,
  binary-collated case-sensitive literal symbol prefixes (including underscore,
  percent, and empty input), and warning propagation when a previous snapshot
  is queried after a failed refresh.
- Dogfood only with an isolated temporary `PROJECT_KB_HOME`. Capture the target
  repository's HEAD, branch, and full porcelain status before and after; never
  execute target modules, scripts, tests, hooks, migrations, or services.
- Check `pkb symbols`, `pkb imports`, and `pkb inspect` against bounded exact
  path/line samples after a successful rebuild.

The current implementation is full-rebuild only. Do not claim incremental
refresh, currentness after arbitrary working-tree edits, generic chunk/search
behavior, runtime dependency tracing, or semantic graph coverage.
