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
- `src/project_kb/git_utils.py` contains Git root resolution helpers.
- `src/project_kb/core/` is reserved for later project logic.

## CLI

- `pkb` is the canonical command.
- `project-kb` is an alias script.
- `pkb version` prints the package version.
- `pkb status --json` returns a structured `NOT_IMPLEMENTED` envelope until the
  project resolver exists.
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
- `registry.sqlite` is the Stage 2 source of truth for project registration.
- Per-project storage directories live under
  `<PROJECT_KB_HOME>/projects/<project_id>/` with `exports/` and `runs/`
  subdirectories.
