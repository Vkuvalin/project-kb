# E4A — AUDIT_4V2: repository search matrix

Search scope: tracked `src/**/*.py` files in the frozen source repository.

## Matrix 1 — Repository classes

Find every module-level class whose short name ends with `Repository`. Report class name, repository-relative defining path, and inclusive class range.

## Matrix 2 — AsyncSession constructor subset

From Matrix 1, find each class whose directly declared constructor contains a parameter annotation referring to `AsyncSession`. Report class name, path, class range, and constructor range.

## Matrix 3 — domain.enums imported-name records

Find every internal imported-name record whose source module is exactly `antique_attribution_ai.domain.enums`. Group by importing path and report the importing path, module, imported names, resolved path, and per-group imported-name record count. Every imported name is one record.

## Matrix 4 — public async repository methods

Find every directly declared public async method in files under `src/antique_attribution_ai/db/repositories/`. Exclude `__init__`, private names, nested functions, and inherited methods. Group by class identity and report names/ranges and group counts.

## Matrix 5 — combined intersection

Intersect Matrix 1, Matrix 2, and classes having a Matrix 4 method. For each class report identity/ranges, constructor dependency, public async method names/ranges/count, and every distinct internal dependency module of its defining file with resolved path.

## Summary and evidence

Report all five matrix counts, Matrix 3 importing-file count, Matrix 4 class count, duplicate identity counts for each matrix, unavailable fields, unresolved and ambiguous records, and source evidence. Canonical identities are the exact path/name/range/import/method combinations described by each matrix. Do not generalize completeness outside the frozen search scope.

Distinguish retrieval substrates in source evidence. Git/source remains answer authority.

## Scope

Do not analyze method bodies, runtime behavior, callers, tests, architecture ownership, Git history, or files outside tracked `src/**/*.py`. Do not modify anything.

## Output

Produce one JSON submission conforming to the task-specific schema named by the session envelope. Do not add prose outside that structure.
