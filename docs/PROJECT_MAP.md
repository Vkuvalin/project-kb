# Project Map

## Runtime Package

- `src/project_kb/cli/` contains the Typer CLI.
- `src/project_kb/output/` contains JSON and text output helpers.
- `src/project_kb/errors.py` contains structured errors.
- `src/project_kb/exit_codes.py` contains process exit code constants.
- `src/project_kb/storage/` contains Project KB home and per-project storage
  directory helpers.
- `src/project_kb/registry/` contains the SQLite registry schema, models, and
  registration service.
- `src/project_kb/resolver/` contains project resolution, project-state policy,
  repository identity, and read-only repository/storage checks.
- `src/project_kb/gating/` contains reusable command-requirement policy and
  structured gate results.
- `src/project_kb/indexing/` contains scanner policy/models, Git candidate
  population, universal bounded ignored-root discovery, parent/final-component
  safe file processing, Python AST extraction, import resolution, rebuild/retry
  orchestration, explicit mutation/access/read failure classes, final evidence
  verification, post-publication bookkeeping outcomes, and query resolution.
- `src/project_kb/snapshot/` contains the versioned per-project SQLite schema,
  validation, atomic publication, and read-only structural queries.
- `src/project_kb/git_utils.py` contains Git root resolution helpers.
- `src/project_kb/core/` remains reserved for later project logic.

## CLI

- `pkb` is the canonical command.
- `project-kb` is an alias script.
- `pkb version` prints the package version.
- `pkb status <project_name> --json` resolves a normalized registry name.
- `pkb status --json` resolves the current directory to its Git root and then
  looks up the registered project.
- `pkb status` classifies registry, repository identity, storage layout, and
  snapshot validity without registering, relinking, indexing, or repairing
  project storage.
- A valid registered project without a snapshot returns
  `REGISTERED_NO_SNAPSHOT` as an expected blocked lifecycle state with
  `error = null` until the first index build.
- A valid published snapshot reports `SNAPSHOT_PRESENT_UNVERIFIED`; this means
  it was coherent when published, not that it is proven current against the
  present working tree.
- A readable but incompatible snapshot reports `SNAPSHOT_REBUILD_REQUIRED`.
  A snapshot newer than stale registry outcome metadata remains available under
  `SNAPSHOT_PRESENT_REGISTRY_WARNING`.
- `pkb index [project_name] [--full] [--json]` performs a full rebuild. The
  default and `--full` forms intentionally use the same engine.
- `pkb symbols [project_name] [--file PATH] [--name PREFIX] [--json]` lists
  exact Python symbols and source ranges. Prefix matching is literal and
  case-sensitive; a missing file is an error rather than an empty result.
- `pkb imports [project_name] [--file PATH] [--json]` lists imports, source
  ranges, bounded local resolution status, file hashes, and extractor
  provenance. A present file with no imports returns an empty successful list.
- `pkb inspect [project_name] --file PATH [--json]` returns file metadata,
  symbols, imports, and parse diagnostics.
- `pkb capabilities --json` reports available command and storage capabilities.
- `pkb register <name> <repo_path> --json` registers a Git repository in
  `registry.sqlite`.
- `pkb projects --json` lists registered projects.
- `pkb relink <name> <new_repo_path> --json` updates a registered repository
  root while keeping project identity and storage.
- `pkb unregister <name> --yes --json` removes a project from tracking and
  deletes its Project KB storage directory.

## Durable Storage

- `PROJECT_KB_HOME` overrides the Project KB storage home.
- On Windows, the default home is `%LOCALAPPDATA%\project-kb`.
- The fallback home is `~/.project-kb`.
- `registry.sqlite` is the source of truth for project registration and stored
  repository fingerprints. Schema version 2 adds nullable fingerprint metadata
  through an additive v1-to-v2 migration.
- Per-project storage directories live under
  `<PROJECT_KB_HOME>/projects/<project_id>/` with `exports/` and `runs/`
  subdirectories.
- `projects/<project_id>/kb.sqlite` is a derived structural snapshot. Rebuilds
  use a SEALED temporary database with a BUILD_SUCCEEDED linked run under
  `runs/`, final repository evidence verification, and one same-volume
  os.replace. Canonical placement is the fact of publication; canonical SQLite
  is not reopened for mutation afterward. Pre-publication failure preserves the
  previous snapshot.
- Snapshot schema version 1 contains `snapshot_meta`, `files`, `pruned_roots`,
  `symbols`, `imports`, `relations`, `parse_diagnostics`, and `index_runs`.
- Snapshot metadata separates the logical structural fingerprint from the
  observation fingerprint and records schema, scanner, policy, and extractor
  versions. snapshot_meta.run_id binds the snapshot to exactly one matching
  project/publication run. Validation checks the complete required manifest and
  sealed build invariants before publication and before a snapshot is queryable.
- Scanner policy is universal in V0. It contains no reference-repository paths
  and has no public per-project configuration; non-standard ignored paths may be
  absent.
- Registry and run-file post-publication bookkeeping are independently reported
  as recorded, not_recorded, or unknown; warnings do not invalidate the
  canonical snapshot.
- Repository fingerprints use local Git metadata such as root commits and a
  hash of the origin URL. Raw remote URLs are not persisted or returned.
