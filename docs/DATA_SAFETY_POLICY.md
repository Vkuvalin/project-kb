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
- Do not create per-project `kb.sqlite` files.
- Do not broadly scan Git history or working trees; the resolver's bounded
  root-commit fingerprint query is the only current history metadata exception.
- Do not index source files.
- Do not call external services, model providers, embedding providers, or LLMs.
- Tests must redirect `PROJECT_KB_HOME` to a temporary directory.
- Tests must not write to real `%LOCALAPPDATA%`.

Any new durable data path must be documented before the application writes
data there.
