# Project Map

## Runtime Package

- `src/project_kb/cli/` contains the Typer CLI.
- `src/project_kb/output/` contains JSON and text output helpers.
- `src/project_kb/errors.py` contains structured errors.
- `src/project_kb/exit_codes.py` contains process exit code constants.
- `src/project_kb/core/` is reserved for later project logic.

## CLI

- `pkb` is the canonical command.
- `project-kb` is an alias script.
- `pkb version` prints the package version.
- `pkb status --json` returns a structured `NOT_IMPLEMENTED` envelope until the
  project resolver exists.
- `pkb capabilities --json` reports the Stage 1 skeleton capabilities.
