# Data Safety Policy

Stage 1 must not create or modify durable Project KB data stores.

Current safety rules:

- Do not create `registry.sqlite`.
- Do not create per-project `kb.sqlite` files.
- Do not scan Git history or working trees.
- Do not index source files.
- Do not call external services, model providers, embedding providers, or LLMs.
- Tests must redirect `PROJECT_KB_HOME` to a temporary directory.
- Tests must not write to real `%LOCALAPPDATA%`.

Later stages must document any durable data paths before writing data there.
