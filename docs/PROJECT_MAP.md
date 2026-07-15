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
  safe file processing, the canonical source-root/module mapper, logical and
  snapshot-bound occurrence identity, Python AST extraction, import resolution,
  rebuild/retry orchestration, explicit mutation/access/read failure classes,
  final evidence verification, post-publication bookkeeping outcomes, and query
  resolution.
- `src/project_kb/snapshot/` contains the versioned per-project SQLite schema,
  v1/v2 validation, v2 proof/currentness verification, atomic publication, and
  read-only structural queries.
- `src/project_kb/git_utils.py` contains the single allow-listed Git runner,
  Git-root helpers, and bounded temporary-index owner used by scanner and
  resolver observations. It removes inherited `GIT_*` controls before applying
  the approved no-system/no-global/no-fetch/no-lock values; the only
  write-shaped Git call populates an internally created managed-storage
  temporary index from staged entries without visibility bits and never targets
  the live repository index or Git directory.
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
- `pkb status [project_name] --verify strong --json` explicitly verifies
  compatible v2 currentness through the authoritative strong proof. Without
  `--verify`, currentness remains `UNVERIFIED`, `truth_claim` is
  `CAPTURED_STABLE`, and `verified_at` is absent. `CURRENT` from strong carries
  `CURRENT_AT_VERIFIED_TIME`, its UTC `verified_at`, scope, exclusions,
  attempts, and proof/verifier contract versions. Strong verification observes
  candidates/status through one visibility-neutral temporary index and seals
  the live index generation without modifying it. The legacy `--verify fast`
  token returns `FAST_VERIFICATION_REMOVED`, recommends strong or a new full
  capture, and performs no proof or mutation. No watcher or automatic refresh
  runs.
- A valid registered project without a snapshot returns
  `REGISTERED_NO_SNAPSHOT` as an expected blocked lifecycle state with
  `error = null` until the first index build.
- A valid published snapshot reports `SNAPSHOT_PRESENT_UNVERIFIED`; this means
  `CAPTURED_STABLE`, not that it is proven current against the present working
  tree.
- A readable but incompatible snapshot reports `SNAPSHOT_REBUILD_REQUIRED`.
  A snapshot newer than stale registry outcome metadata remains available under
  `SNAPSHOT_PRESENT_REGISTRY_WARNING`.
- Explicit verification can additionally report `OK`/`CURRENT`,
  `SNAPSHOT_STALE`, `SNAPSHOT_CHANGED_DURING_CHECK`, or
  `SNAPSHOT_VERIFICATION_ERROR` while keeping availability and currentness as
  separate fields.
- Attempting strong verification on valid legacy v1 reports
  `SNAPSHOT_LEGACY_REINDEX_REQUIRED`, keeps bounded legacy reads available, and
  recommends `REINDEX_FOR_V2_CURRENTNESS`; no verification duration or
  timestamp is fabricated.
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
  root while keeping project identity and storage. Every explicit relink rotates
  the active binding generation and requires a rebuild; the prior generation's
  snapshot remains unavailable after a failed reindex.
- `pkb unregister <name> --yes --json` removes a project from tracking and
  deletes its Project KB storage directory.

## Durable Storage

- `PROJECT_KB_HOME` overrides the Project KB storage home.
- On Windows, the default home is `%LOCALAPPDATA%\project-kb`.
- The fallback home is `~/.project-kb`.
- `registry.sqlite` is the source of truth for project registration and stored
  repository fingerprints. Registry schema version 3 adds active and last-
  snapshot binding generations through additive migrations from v1 or v2.
  Migration orders historical relink and index-outcome events; an unconfirmed
  post-relink success or incomplete history remains rebuild-required.
- Per-project storage directories live under
  `<PROJECT_KB_HOME>/projects/<project_id>/` with `exports/` and `runs/`
  subdirectories.
- `projects/<project_id>/kb.sqlite` is a derived structural snapshot. Rebuilds
  use a SEALED temporary database with a BUILD_SUCCEEDED linked run under
  `runs/`, final repository evidence verification, and one same-volume
  os.replace. Canonical placement is the fact of publication; canonical SQLite
  is not reopened for mutation afterward. Pre-publication failure preserves the
  previous snapshot. Post-publication recovery separately classifies active,
  relink-quarantined, and unconfirmed publication using root, identity hash,
  binding generation, v2 run/snapshot identity, and the active registry binding.
- Legacy snapshot schema v1 remains recognizable and readable through bounded
  v1 semantics. Every new full rebuild writes semantic schema v2 from source;
  v1 rows are never migrated or backfilled in place.
- Snapshot schema v2 adds active repository binding, canonical module/source-root
  fields, logical and snapshot-bound occurrence identity, explicit proof rows,
  and versioned proof/verifier/module/occurrence contracts.
- The module mapper applies explicit roots, supported unambiguous packaging
  roots, then convention only when root packaging evidence is absent. Supported,
  unsupported, and unreadable evidence remain distinct; `setup.cfg`, `setup.py`,
  unsupported backends, and unreadable `pyproject.toml` are non-exact and are
  never executed.
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
  hash of the origin URL. Raw remote URLs are not persisted or returned. Live
  fingerprint observations are sealed before and after currentness proof work;
  a changed origin hash is an identity mismatch and requires no network access.
