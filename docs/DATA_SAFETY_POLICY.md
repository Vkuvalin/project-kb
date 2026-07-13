# Data Safety Policy

The application may create and modify only the Project KB registry and managed
project-storage directories described here. Source repositories remain
read-only.

Current safety rules:

- Store Project KB data under `PROJECT_KB_HOME`, `%LOCALAPPDATA%\project-kb` on
  Windows, or `~/.project-kb` as the fallback.
- `registry.sqlite` is allowed and is the source of truth for project
  registration and lightweight repository fingerprint metadata.
- Opening an existing supported registry may perform the additive v1-to-v2
  schema migration. A valid legacy record may receive a one-time fingerprint
  initialization and registry event.
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
- Repository identity checks may run bounded local Git metadata commands for
  Git-root/common-dir resolution, registration HEAD, and root commits. They
  must not fetch, contact remotes, or scan working-tree file contents.
- Origin URLs may be read locally only long enough to compute a SHA-256 hash.
  Raw origin URLs must not be stored, logged, or returned.
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
- Python extraction uses the standard-library AST parser. Target modules,
  scripts, tests, hooks, migrations, and application services must never be
  imported or executed by indexing.
- A temporary snapshot is fully built and sealed before validation:
  snapshot_meta.build_status is SEALED and the linked index run is
  BUILD_SUCCEEDED. Final candidate and processed-object evidence verification
  follows validation. Repository change retries once; a second detection returns
  REPO_CHANGED_DURING_SCAN. One same-volume os.replace is the only publication
  commit point. The canonical SQLite file is not reopened or mutated after
  replacement. Pre-publication failures preserve the previous snapshot.
- After publication, registry and run-file bookkeeping are reported separately
  as recorded, not_recorded, or unknown. Incomplete or uncertain bookkeeping
  returns warnings, keeps the snapshot queryable, and must not record
  INDEX_FAILED for the published snapshot.
- Validation checks SQLite integrity, required tables/columns/indexes, one
  metadata row, its exact run_id, exactly one matching publication run, matching
  project and published snapshot IDs, compatible sealed/build-success states,
  foreign keys, cross-table invariants, and scanner/policy/extractor
  compatibility. Readable incompatible snapshots require a full rebuild rather
  than an in-place migration.
- Do not call external services, model providers, embedding providers, or LLMs.
- Tests must redirect `PROJECT_KB_HOME` to a temporary directory.
- Tests must not write to real `%LOCALAPPDATA%`.

Any new durable data path must be documented before the application writes
data there.
