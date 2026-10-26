# E4A — AUDIT_1V2: WorkflowStateRepository structure

## Subject

`WorkflowStateRepository`

## Tool-neutral question

Return only these structural facts:

1. Defining repository-relative path and inclusive AST class declaration range.
2. Constructor name, async status, ordered parameters with annotations/defaults, return annotation, and inclusive declaration range.
3. Every directly declared public method: name, async status, ordered parameters with annotations/defaults, return annotation, and inclusive declaration range.
4. Every outgoing imported-name record in the defining file: module, imported name, internal/external classification, and resolved repository path when internal.
5. Completeness counts and any unavailable requested fields.
6. Source evidence supporting the structured facts.

## Definitions

A public method is directly declared in the subject class and its name does not begin with `_`. Report `__init__` separately. Exclude inherited methods, imported callables, nested functions, and call sites. A range is the inclusive AST declaration range. `is_async` is true only for `async def`. An import is internal only when it resolves to a tracked file inside the frozen repository; standard-library and third-party imports are external.

Formatting-only whitespace differences are immaterial. Parameter order is material. The schema's `kind` field is a uniform serialization aid and is not a separately scored AUDIT_1V2 fact.

## Scope

Do not analyze behavior, callers, usages, reverse imports, tests, workflow impact, architecture ownership, workflow update-field policy, or Git history. Do not modify anything.

## Output

Produce one JSON submission conforming to the task-specific schema named by the session envelope. Do not add prose outside that structure. State unavailable facts explicitly rather than inventing them.
