# Data Safety Policy

The application may create and modify only the Project KB registry and managed
project-storage directories described here. Source repositories remain
read-only.

Current safety rules:

- Store Project KB data under `PROJECT_KB_HOME`, `%LOCALAPPDATA%\project-kb` on
  Windows, or `~/.project-kb` as the fallback.
- `registry.sqlite` is allowed and is the source of truth for project
  registration and lightweight repository fingerprint metadata.
- Opening an existing supported registry may perform an additive v1/v2-to-v3
  schema migration. A valid legacy record receives binding-generation metadata
  and one migration event. Migration uses durable historical relink and
  index-outcome ordering; a relink without a confirmed later successful index,
  ambiguous ordering, incomplete history, or an existing relink-required marker
  remains fail-closed without labeling valid legacy data corrupt.
- A status lookup does not create a missing Project KB home or registry and
  does not create or repair per-project storage.
- Registry files and managed project-storage paths must not resolve through
  unsafe symlinks or junctions. Redirects outside expected managed locations
  are rejected rather than followed.
- Registered projects may create
  `<PROJECT_KB_HOME>/projects/<project_id>/exports/` and
  `<PROJECT_KB_HOME>/projects/<project_id>/runs/`.
- A registered project may create one derived structural snapshot at
  `<PROJECT_KB_HOME>/projects/<project_id>/kb.sqlite`. Temporary build and run
  records stay under the same managed project storage.
- `pkb unregister <name> --yes` may delete only the registered Project KB
  project's exact expected storage directory.
- An explicit idempotent `pkb register` refresh may recreate missing storage
  only at `<PROJECT_KB_HOME>/projects/<project_id>`; mismatched registry paths
  must be refused.
- Do not delete or modify source repositories during unregister.
- Repository identity, scanner, status, and currentness checks may run only
  allow-listed local read-only Git observations through the shared runner. The
  runner accepts exact argument profiles, removes inherited `GIT_*` controls,
  then applies no-system/no-global config, local `core.fsmonitor`, paging,
  lazy-fetch, and optional-lock controls; the local origin read does not follow
  config includes. These checks must not fetch, contact remotes, execute
  repository hooks/helpers, or create or modify files in the source repository's
  live index, worktree Git directory, or common Git directory. This prohibition
  includes `sharedindex.*`, index lock/sidecar files, and repository config. The
  only write-shaped Git operation is exact staged-entry population of an
  internally created and automatically removed temporary `GIT_INDEX_FILE` under
  managed project storage. That command applies a non-persistent
  `core.splitIndex=false` override so repository-local split-index configuration
  cannot redirect temporary-index storage back into the live Git common
  directory.
- Origin URLs may be read locally only long enough to compute a SHA-256 hash.
  Raw origin URLs must not be stored, logged, or returned. A changed origin hash
  is a repository-identity mismatch; verification samples the bounded live
  fingerprint locally before and after proof work and never fetches.
- Structural indexing starts from Git tracked files plus untracked non-ignored
  files. V0 may additionally check only exact roots from a universal technical
  prune list. Git-provided paths and spelling take priority over policy
  discovery. There are no reference-repository defaults and no public
  per-project policy support; non-standard ignored runtime paths may be absent.
  These checks perform no global ignored-tree scan, deep traversal, or
  Git-history crawl.
- Hard-secret path policy is applied before content access. Secret content is
  not opened, decoded, or content-hashed; only safe relative-path and metadata
  classification may be retained when the path enters candidate population.
  Strong currentness verification treats hard-secret content as an explicit
  exclusion and must not open or hash it.
  Ignored .env files commonly remain absent because V0 does not enumerate
  ignored files. Real .env variants and exact credential/token basenames are
  hard-secret, while .env.example, .env.sample, .env.template, and .env.dist
  remain eligible for text indexing.
- Symlink, junction, and reparse candidates are metadata-only and are not
  intentionally followed. Before opening, every path component and canonical
  parent containment are checked. Parent-component lstat identities are
  captured, checked again immediately before open, and checked after the bounded
  read; final-object identity is also checked before and after. Platforms with
  O_NOFOLLOW use it for the final component. Windows lacks equivalent
  Python-level kernel enforcement, so the implemented boundary is fail-closed
  pre/post reparse and identity checks, not an absolute claim against a
  privileged concurrent filesystem attacker.
- Proven repository mutation is distinct from static path refusal and ordinary
  access/read failure. Only changed identities, disappeared candidates,
  newly introduced redirects, changed evidence, or changed candidate population
  use the bounded repository-change retry. Static unsafe paths, permission or
  sharing errors, and ordinary I/O failures fail closed without being mislabeled
  REPO_CHANGED_DURING_SCAN.
- Text inspection is bounded and strict: UTF-8 and UTF-8 BOM are supported;
  unsupported encodings, binary data, oversized files, and unsupported formats
  remain metadata-only.
- Source text may be read transiently for hashing and structural extraction but
  is not duplicated into SQLite. The snapshot stores hashes, line counts,
  positions, and derived facts, not full content or content history.
- Python extraction uses the standard-library AST parser. Indexing and
  currentness verification never import target modules. Target modules,
  scripts, tests, hooks, migrations, and application services must never be
  imported or executed.
- A temporary snapshot is fully built and sealed before validation:
  snapshot_meta.build_status is SEALED and the linked index run is
  BUILD_SUCCEEDED. Final candidate and processed-object evidence verification
  follows validation. Repository change retries once; a second detection returns
  REPO_CHANGED_DURING_SCAN. One same-volume os.replace is the only publication
  commit point. The canonical SQLite file is not reopened or mutated after
  replacement. Pre-publication failures preserve the previous snapshot.
- After publication, registry and run-file bookkeeping are reported separately
  as recorded, not_recorded, or unknown. Incomplete or uncertain bookkeeping
  returns warnings and keeps the snapshot queryable only when root, identity
  hash, binding generation, v2 run/snapshot identity, and active registry
  binding still match. A snapshot physically replaced before a concurrent
  relink is quarantined/rebuild-required and is not reported as usable success.
- Post-publication currentness verification is source-read-only and opt-in.
  Strong mode uses the visibility-neutral observation boundary: one stable live
  Git staged-entry projection is rebuilt in an isolated temporary index under
  managed project storage without assume-unchanged or skip-worktree bits; the
  normalized live paths are retained as diagnostics before candidate and status
  observation. Live raw-index path/content/file-identity/timestamp
  evidence plus logical staged-entry and visibility projections seal the capture. Stable
  visibility flags are `UNVERIFIED`; generation or
  visibility instability retries and then fails closed rather than yielding
  `CURRENT`, while a hidden write remains visible through the neutral view even
  if the live flag undergoes ABA. Strong mode compares
  the v2 proof manifest through scanner-owned safe reads and a bounded two-pass
  proof seal; stable raw whole-repository status is diagnostic rather than an
  independent semantic stale predicate. Every strong check finishes
  with a terminal candidate, repository-state, binding, live-identity, and
  scanner-owned object proof, including clean-to-clean HEAD changes and dirty
  same-status mutations. Hard-secret and pruned content remains explicitly
  excluded, not verified. An unstable observation retries once, then reports
  CHANGED_DURING_CHECK rather than guessing CURRENT. Every completed strong v2
  verification outcome includes UTC `verified_at`; `CURRENT` is explicitly
  `CURRENT_AT_VERIFIED_TIME`. Default semantic-v2 status is `UNVERIFIED` with
  `CAPTURED_STABLE`. The removed fast token performs no repository, snapshot, or
  registry inspection or mutation and includes neither timestamp nor timing.
  Unexecuted valid-v1 strong verification likewise fabricates neither.
- Validation checks SQLite integrity, required tables/columns/indexes, one
  metadata row, its exact run_id, exactly one matching publication run, matching
  project and published snapshot IDs, compatible sealed/build-success states,
  foreign keys, cross-table invariants, and scanner/policy/extractor
  compatibility. Version identification precedes v1/v2 manifest checks. V2
  additionally validates active repository binding, proof/file consistency,
  source-root/module mapping, verifier contracts, and snapshot-bound occurrence
  identity. Readable incompatible or wrong-binding snapshots require a full
  rebuild rather than an in-place migration.
- Relink never deletes or rewrites the canonical snapshot in place. Every
  explicit relink rotates the registry binding generation; the snapshot
  generation remains unchanged across failed reindex attempts, so trusted facts
  stay unavailable until a full source reindex publishes and records the active
  binding.
- Do not call external services, model providers, embedding providers, or LLMs.
- Tests must redirect `PROJECT_KB_HOME` to a temporary directory.
- Tests must not write to real `%LOCALAPPDATA%`.

Any new durable data path must be documented before the application writes
data there.
