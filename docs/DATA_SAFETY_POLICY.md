# Data Safety Policy

Stage 2 may create and modify only the Project KB registry and project storage
directories described here.

Current safety rules:

- Store Project KB data under `PROJECT_KB_HOME`, `%LOCALAPPDATA%\project-kb` on
  Windows, or `~/.project-kb` as the fallback.
- `registry.sqlite` is allowed and is the source of truth for project
  registration.
- Registered projects may create
  `<PROJECT_KB_HOME>/projects/<project_id>/exports/` and
  `<PROJECT_KB_HOME>/projects/<project_id>/runs/`.
- `pkb unregister <name> --yes` may delete only the registered Project KB
  project storage directory.
- Do not delete or modify source repositories during unregister.
- Do not create per-project `kb.sqlite` files.
- Do not scan Git history or working trees.
- Do not index source files.
- Do not call external services, model providers, embedding providers, or LLMs.
- Tests must redirect `PROJECT_KB_HOME` to a temporary directory.
- Tests must not write to real `%LOCALAPPDATA%`.

Later stages must document any durable data paths before writing data there.
