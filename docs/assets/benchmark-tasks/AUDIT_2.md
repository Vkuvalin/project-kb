# E4A — AUDIT_2: multi-symbol dependency surface

## Subjects

1. `StagedMediaExpiryGuard`
2. `StagedMediaCleanupService`
3. `PhotoRepository`

## Tool-neutral question

For each subject, return:

1. Defining repository-relative path and inclusive AST class declaration range.
2. Constructor name, async status, ordered parameters with annotations/defaults and positional/keyword boundaries, return annotation, and inclusive declaration range.
3. Every directly declared public method with the same signature and range fields.
4. Every outgoing internal imported-name record from the defining file: module, imported name, and resolved repository path.

Then derive direct subject edges and shared internal dependency modules. A direct subject edge exists when one subject's defining file imports another named subject. A shared internal dependency module is imported by at least two distinct subject files; group imported names by source subject and retain repeated records.

Report per-subject counts, aggregate counts, unavailable fields, unresolved or ambiguous records, and source evidence.

## Definitions

Public methods are directly declared names that do not begin with `_`; report `__init__` separately. Exclude private/inherited methods, imported callables, nested functions, module functions, callers, and call sites. A range is the inclusive AST declaration range. `is_async` is true only for `async def`. Internal imports resolve to tracked files in the frozen repository. Parameter order and parameter-kind boundaries are material; collection ordering is not.

## Scope

Do not analyze method bodies, runtime behavior, reverse references, tests, expiry/cleanup/persistence semantics, architecture ownership, hypothetical impact, or Git history. Do not modify anything.

## Output

Produce one JSON submission conforming to the task-specific schema named by the session envelope. Do not add prose outside that structure. Never invent an unavailable fact.
