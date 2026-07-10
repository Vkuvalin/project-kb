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
- `src/project_kb/git_utils.py` contains Git root resolution helpers.
- `src/project_kb/core/` is reserved for later project logic.

## CLI

- `pkb` is the canonical command.
- `project-kb` is an alias script.
- `pkb version` prints the package version.
- `pkb status <project_name> --json` resolves a normalized registry name.
- `pkb status --json` resolves the current directory to its Git root and then
  looks up the registered project.
- `pkb status` classifies registry, repository identity, storage layout, and
  snapshot-placeholder state without registering, relinking, or repairing
  project storage.
- A valid registered project without a snapshot returns
  `REGISTERED_NO_SNAPSHOT` as an expected blocked lifecycle state with
  `error = null` until indexing exists.
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
- Repository fingerprints use local Git metadata such as root commits and a
  hash of the origin URL. Raw remote URLs are not persisted or returned.
