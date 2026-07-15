# Stage 6 Currentness Simplification and Task Snapshot Lifecycle Architecture Audit

Boundary: this report is preserved analysis, not source of truth. It does not approve architecture, implementation, code review, commit, merge, release, deployment, or another index run.

## 1. Metadata and inspected boundary

### Task and route

- Initial task envelope: `docs/codex/tasks/stage-6-currentness-lifecycle-architecture-audit.md`.
- Intent: deep read-only architecture audit, currentness reassessment, lifecycle design, and report only.
- Route: `L4 Route Exploration`, resolved by `.agents/skills/reasoning-patterns/SKILL.md` to `docs/codex/reasoning/L4_shallow_tree_of_thoughts.md`. No reroute was needed because the work remained a comparison of several architecture routes rather than implementation.
- Procedures used: `audit-only`, `report-writing`, and `subagent-routing`.
- Independent read-only lenses: `architecture_auditor` for ownership/currentness/lifecycle boundaries and `test_auditor` for retained/obsolete tests and validation safety. Neither agent wrote files or approved a direction.
- Audit date: 2026-07-15, Europe/Moscow.

### Base-state facts

- HEAD: `5af6736257f95bb958534b7cc74525200d81dc01` (the expected base).
- Branch: `main`, tracking `origin/main`, ahead/behind `+0/-0`.
- Staging: empty. Porcelain-v2 entries were `.M`, never `M.` or `MM`.
- Worktree: 28 tracked modifications and 10 untracked Stage 6 implementation/test paths. Their path set exactly matches the 38 paths named by the permitted v6 diff artifact; no unrelated tracked path was found.
- Tracked diff size: 3,379 insertions and 277 deletions. The 10 untracked Stage 6 files contain 5,458 lines: 1,969 production lines and 3,489 test/fixture lines.
- This task did not reset, stage, stash, checkout, commit, branch, push, or recreate prior work.

### Permitted evidence inventory

All six permitted artifacts are present:

| Artifact | Status |
|---|---|
| `docs/codex/reports/stage-6-dogfood-stale-audit-report.md` | present |
| `docs/codex/reports/stage-6-dogfood-antique-attribution-ai-source-before.json` | present |
| `docs/codex/reports/stage-6-dogfood-antique-attribution-ai-git-storage-before.json` | present |
| `docs/codex/reports/stage-6-dogfood-antique-attribution-ai-index.json` | present |
| `docs/codex/reports/stage-6-dogfood-antique-attribution-ai-fast-1.json` | present |
| `docs/codex/diff/stage-6-trust-substrate-implementation-v6.diff` | present; ignored and therefore discoverable with `rg --files -uu` |

No superseded Stage 6 Stage Brief, OPS task, fix-loop task, ExecPlan, cumulative implementation report, or earlier implementation audit was opened directly. The explicitly permitted dogfood report contains historical references, but it was used only as preserved evidence and not as source of truth.

### Inspected

- The current uncommitted implementation under `src/project_kb/{cli,gating,indexing,registry,resolver,snapshot}`.
- The directly related Stage 6 tests and fixtures.
- Current durable contracts in `README.md`, `docs/DATA_SAFETY_POLICY.md`, `docs/DEVELOPMENT_CHECKLIST.md`, `docs/PROJECT_CONTEXT.md`, and `docs/PROJECT_MAP.md`.
- The six permitted evidence artifacts.
- The published `antique-attribution-ai` v2 snapshot and registry through production read-only status/currentness paths and before/after SHA-256 checks.
- Focused safe tests after confirming that the audit process had no inherited `GIT_*` redirectors.

### Not inspected or performed

- No new real index, target execution, secret-content read/hash, network, or fetch.
- No snapshot or registry mutation; before/after hashes were identical.
- No prototype of a lifecycle storage model.
- No attempt to prove absolute behavior under an uncooperative privileged concurrent writer.
- No future structural-semantics or heuristic rename/move design.

## 2. Executive recommendation

### Recommended route

Use a bounded `B -> C` sequence:

1. Close Stage 6 with **Route B: strong-only authoritative verification**. Remove the ability of fast verification to return `CURRENT` and retire its status-fingerprint stale predicate. Keep current semantic-v2 capture, proof manifest, safe object reads, repository binding, atomic publication, and strong verification.
2. Build a separate **Route C: capture-first Task Snapshot Lifecycle Stage**. A full immutable generation becomes the normal way to obtain current structural facts; strong verification remains an optional point-in-time diagnostic, not a prerequisite after every successful capture.

Do not combine these into one implementation task.

### Recommended storage model

Use **Model 4: one immutable SQLite file per generation plus managed transactional pointers**. Store immutable generation files under project-owned storage and keep project/workspace/task/generation ownership plus active pointers in the registry (or a dedicated project-control database) under one transaction boundary.

### Recommended Stage boundaries

- Stage 6 closure: stop public fast `CURRENT`; remove fast-only decision branches, output/help claims, and tests; keep strong and everything needed by strong/full capture. Do **not** remove the temporary-index/index-generation/split-index machinery in the minimum closure because current strong verification still calls it.
- Next Stage: generation storage, workspace/task identity, lifecycle transitions, comparison, cleanup/recovery, and dogfood. This Stage may later replace the strong observer and remove the temporary-index machinery, but only with focused equivalence and no-live-Git-mutation validation.

### Compatibility result

- Current semantic-v2 snapshots remain valid and queryable.
- Snapshot `b03ed877477c4445b0347e7695e98402` can be retained.
- No schema, proof-contract, verifier-contract, or full-reindex bump is required merely to remove public fast verification.
- Removed fast fields may remain validated legacy/provenance fields in schema v2.
- Legacy v1 remains bounded-readable but cannot supply v2 strong currentness or become a new task baseline without a full v2 capture.

### Highest findings

| Severity | Finding | Consequence |
|---|---|---|
| blocking | Test Git helpers inherit parent `GIT_*` redirectors instead of applying production's deny-by-default environment (`tests/conftest.py:34`, `tests/_stage6_support.py:11`). | Broad or uncontrolled test execution can escape disposable repositories. Sanitize helpers before Stage 6 closure validation. The focused audit run was made only after confirming this process had no `GIT_*` variables. |
| high | Fast compares status fingerprints created by different observer protocols and reproducibly returns false `STALE` on Windows CRLF worktrees. | False reindex advice and false blocking of currentness-gated consumers. |
| high | Fast does not compare persisted object content; it cannot independently establish Project KB's high-trust content equality. | The product carries a second, weaker truth claim with little actual consumer value. |
| high | Current project identity combines logical project and one checkout root; there is no workspace/worktree/task/generation entity. | The proposed lifecycle cannot safely support linked worktrees or parallel write-capable tasks without an additive ownership model. |
| high | Publication/currentness cannot be absolutely linearizable against arbitrary writers without an honored external lock. | Public truth must be bounded (`CAPTURED_STABLE`, `CURRENT_AT_VERIFIED_TIME`), never permanent `CURRENT`. |

### Recommendation record

- Repository evidence: sections 3–5 and 8–9.
- Alternatives considered: Routes A–D and storage Models 1–4.
- Main trade-off: retain a proven seconds-scale strong path now, accepting its temporary-index implementation complexity until a separately reviewed replacement exists.
- Principal failure mode avoided: deleting machinery that strong still executes and silently weakening concurrency/visibility checks.
- Confidence: high for the Stage 6 closure and current-v2 compatibility; medium-high for the lifecycle model pending Command Center decisions on project/workspace identity and retention limits.

## 3. Current implementation map and complexity inventory

### Ownership and call paths

| Concern | Current owner and call path | Facts |
|---|---|---|
| Full capture | `cli.app.index_command()` (`src/project_kb/cli/app.py:305`) -> `IndexService.index()` (`indexing/service.py:54`) -> `git_candidates()` / `capture_repo_state()` (`service.py:114-126`) -> `scan_repository()` (`service.py:117`) -> `verify_scan_evidence()` (`service.py:124`) | `pkb index` and `pkb index --full` are two CLI spellings for one full engine; response `effective_mode` is always `full`. |
| Semantic-v2 build | `write_snapshot()` (`snapshot/database.py:347-667`) using `SCHEMA_SQL_V2` (`snapshot/schema_v2.py`) | Writes one sealed SQLite with structural facts, proof rows, versions, repository binding, run data, and two fingerprints. |
| Proof manifest | `_proof_rows()` (`snapshot/database.py:684-730`), schema table at `snapshot/schema_v2.py:152` | Safe text gets `CONTENT_HASH`; non-text bounded objects get `BOUNDED_METADATA`; hard-secret/pruned content is explicitly excluded. |
| Validation/publication | `validate_snapshot()` (`snapshot/database.py:733-852`) -> final live state/evidence check (`indexing/service.py:174-181`) -> `publish_snapshot()` (`snapshot/database.py:1555-1567`) -> registry/run bookkeeping -> `_canonical_publication_state()` (`indexing/service.py:454`) | Temp DB is sealed and validated, then `os.replace` publishes it. Post-replace reconciliation distinguishes active, quarantined, and unconfirmed states. |
| Strong verification | `ProjectStatusService.status()` (`resolver/project.py:59`) -> snapshot validation (`project.py:247`) -> `verify_snapshot_currentness()` (`project.py:366`, `snapshot/currentness.py:116`) -> `_strong_object_deltas()` (`currentness.py:842`) | Compares proof-manifest objects by scanner-owned safe reads; checks candidates, HEAD, binding, identity, visibility, and several stability seals. |
| Fast verification | Same status call -> `VerificationMode.FAST` (`snapshot/currentness.py:45`) -> repository/candidate deltas -> fast seal (`currentness.py:252`) -> status-fingerprint and clean-state predicates (`currentness.py:383-418`) | Never calls `_strong_object_deltas()`. It loads proof rows for candidate/exclusion metadata but does not compare safe-text content hashes. |
| Temporary neutral observer | `_capture_observation()` (`snapshot/currentness.py:614`) -> `capture_repo_observation()` (`indexing/scanner.py:142-212`) -> `temporary_git_index()` / `live_git_index_generation()` (`git_utils.py:188-289`) | Reconstructs staged entries into isolated `GIT_INDEX_FILE`, strips visibility/split-index state, and seals a live-index generation. **Both fast and strong currently use this path.** |
| Status/resolver/gating | `ProjectStatusService._status_resolved_project()` (`resolver/project.py:209-469`), `_availability()` (`project.py:590`), `evaluate_gate()` (`gating/policy.py:22`) | Explicit verification maps `CURRENT` to `OK`, `STALE` to `SNAPSHOT_STALE`, and current-only gates require exact string `CURRENT` (`gating/policy.py:50`). |
| Registry binding | Registry v3 `projects` row (`registry/schema.py:8-42`), `repo_binding_generation` / `snapshot_binding_generation`; relink rotates at `registry/service.py:211-275` | One project row owns one normalized checkout root and one storage directory. Binding and live identity are rechecked around verification/query/publication. |
| Snapshot storage | `projects/<project_id>/kb.sqlite`, temp runs under `projects/<project_id>/runs/`; path ownership in `storage/home.py` and registry service | One mutable canonical pathname, replaced atomically with a newly sealed database. |
| Query selection | `symbols` / `imports` / `inspect` -> `QueryService._reader()` (`indexing/query.py:35-91`) -> `SnapshotReader` (`snapshot/database.py:1633`) | Query selects the one canonical `kb.sqlite`. It requires `can_use_snapshot`, not `can_search`, and does not run fast or strong verification (`query.py:36-37`). |
| Query binding safety | `SnapshotReader._validate_active_binding()` before and after each read (`snapshot/database.py:1730-1783`) | Prevents relink/registration changes from silently exposing the wrong snapshot. |
| Cleanup/recovery | `IndexService._safe_cleanup()` (`indexing/service.py:634`), temporary-directory context manager (`git_utils.py:188`), `_canonical_publication_state()` and `_published_fallback()` (`indexing/service.py:454,524`) | Pre-publication temp cleanup is best-effort; ambiguous post-publication state is revalidated. There is no generation orphan/pointer recovery because generations do not yet exist. |

### Quantitative inventory

Facts measured from the current files:

- `snapshot/currentness.py`: 904 lines.
- `git_utils.py`: 395 lines.
- `indexing/scanner.py`: 1,156 lines.
- New Stage 6 production modules (`identity.py`, `module_map.py`, `currentness.py`, `schema_v2.py`): 1,969 lines.
- New Stage 6 test/fixture files: 3,489 lines.
- `test_stage6_currentness.py`: 1,195 lines; `test_stage6_safety.py`: 573 lines; `test_stage6_snapshot_trust.py`: 963 lines; `test_stage6_identity.py`: 398 lines.
- At least 18 named tests directly mention fast, temporary-index, split-index, visibility-neutral, or index-flag behavior. Their contiguous test regions are roughly 700 lines, excluding mixed fast/strong parameterizations and shared race tests.
- Snapshot v2 `snapshot_meta` has 25 columns, including eight explicit version/contract fields: schema, scanner, policy, extractor, proof, verifier, module-map, and occurrence versions.
- One public capture service path exists. One public status path has two verification branches. Three public query commands use the canonical snapshot without currentness verification.
- A successful fast attempt performs three temporary-index repository observations and three binding/identity seals.
- A successful strong attempt performs five temporary-index repository observations and three full object-proof passes (initial, seal, and terminal) plus repeated binding/identity checks.

### Dependency split

Fast-only today:

- `VerificationMode.FAST` public value and CLI help.
- Fast third observation/seal at `snapshot/currentness.py:252-279`.
- `git_status_fingerprint_changed` -> `STALE` and the clean-state prerequisite at `currentness.py:383-418`.
- Fast-specific result reason/timing expectations, docs, and tests.
- False-`STALE` reindex action reached from the fast status projection.

Shared with current strong and therefore not safely removable in the minimum closure:

- `capture_repo_observation()`.
- `temporary_git_index()`, `TemporaryGitIndex`, live-index generation sealing, temporary `update-index`, split-index isolation, and cleanup.
- visibility-flag rejection, candidate population comparison, binding/live-identity seals, bounded retry, and repository-observation stability.

Shared with full capture/high-trust snapshots and required to retain:

- safe Git command boundary, `git_candidates()`, live `capture_repo_state()`, scanner safe reads, content hashing, evidence verification, semantic-v2 schema, proof manifest, repository binding, validation, atomic publication, and query binding rechecks.

Interpretation: deleting only the fast branches is small and safe. Deleting the neutral-index subsystem at the same time is a strong-verifier rewrite, not fast cleanup.

## 4. Verified dogfood facts

### Confirmed hypotheses

1. Semantic-v2 snapshot `b03ed877477c4445b0347e7695e98402` was successfully and atomically published for `antique-attribution-ai`.
2. The index response reports schema 2, scanner `stage6-v2`, proof/verifier contract 3, size 7,057,408 bytes, `POST_PUBLICATION_RECORDED`, and total 3,250 ms.
3. The permitted dogfood report records strong `CURRENT` twice at 3,404 ms and 3,885 ms. This audit freshly reproduced strong `CURRENT` in 4,503 ms with no deltas.
4. Preserved fast returned false `STALE` in 1,944 ms. This audit freshly reproduced the same result in 1,955 ms with the same before/after hashes and no mismatch path.
5. Publication stored live-index status; verification used a reconstructed visibility-neutral index. Code confirms the protocol split at `indexing/service.py:114-126` versus `snapshot/currentness.py:614` / `indexing/scanner.py:142-212`.
6. The neutral observer reported exactly 83 ` M` records. The permitted dogfood report records a byte-level comparison showing all 83 are CRLF worktree versus LF blob differences and none has another byte difference.
7. A status payload consists of status/path projection bytes, not persisted file contents. A content change can leave the same status/path projection; the strong regression at `tests/test_stage6_currentness.py:619` explicitly covers unchanged status fingerprint with changed content.
8. Temporary reconstruction, raw/logical index generation sealing, visibility controls, split-index isolation, Git-storage safety, ABA checks, and observer parity create substantial production and test surface.
9. Full capture (3.250 s), preserved strong (3.404/3.885 s), and fresh strong (4.503 s) are all seconds-scale. Fast saved roughly 1.3–2.6 seconds in this evidence set.
10. The supplied product target is one high-trust quality level; no inspected query feature requires a weaker fast proof.

### Corrected or bounded interpretations

- The dogfood failure is not a semantic snapshot defect, publication defect, registry mismatch, or target change. It is a false-positive currentness defect caused by comparing non-equivalent observer protocols.
- Current strong verification is content-authoritative only within its declared proof scope. `.venv`, hard-secret content, ignored content, and pruned content remain excluded.
- Fresh strong `CURRENT` is not a permanent truth. It is evidence for `CURRENT_AT_VERIFIED_TIME` under bounded stability checks.
- The preserved Git-storage evidence proves unchanged observed endpoints, not the impossibility of a transient mutation that was restored between observations.

### Immutability check

Before and after the fresh strong/fast checks:

- registry SHA-256: `37D298AA063B94EA11B1E1083FE2E33E457D107F22F42D82B2FA2A36867D2797`;
- canonical snapshot SHA-256: `6B44FE20B847A5BBD4693547FBAB99A935A54406910C95C4C40B03643D4545B6`.

Both remained identical. No real index was run.

## 5. Meaning and value of current fast mode

### What fast inspects

Fast currently inspects:

- snapshot compatibility and repository binding supplied by status validation;
- active registry binding and live repository fingerprint before/after/seal;
- HEAD and branch;
- candidate paths and `TRACKED` / `UNTRACKED` / `POLICY_DISCOVERED` populations;
- live index visibility flags;
- raw porcelain-v1 status bytes through the temporary reconstructed index, reduced to SHA-256;
- live index path/content/file identity/timestamp plus staged-entry/visibility projections as an index-generation seal;
- proof-manifest rows only to reconstruct the expected candidate set and exclusions.

### What fast does not inspect

Fast does not compare:

- safe-text content hashes;
- bounded metadata evidence for each object;
- parse state, module identity, symbols, imports, relations, or diagnostics;
- ignored/pruned/hard-secret content;
- semantic equivalence of two different Git observer protocols.

It never calls `_strong_object_deltas()`.

### Claimed versus actual guarantee

Durable docs call fast a conservative clean-working-tree proof (`README.md:34-43`) and state that both modes can report `CURRENT` (`README.md:44-50`). In practice its affirmative claim is:

> under matching binding/identity, stable reconstructed-index observations, same HEAD/candidate population, no visibility flags, and both persisted/current status hashes equal to the empty-status hash, no change was detected by this Git status protocol.

That is not the same as Project KB object-content equality. The dogfood shows that even the negative result is observer-dependent.

### Original and actual use cases

Interpretation from code/docs:

- Original motivation: a cheaper explicit status probe for clean repositories, capable of unlocking `SNAPSHOT_CURRENT` gates without reading every safe object.
- Actual public use: `pkb status --verify fast`; it changes status/gating fields and can recommend reindex.
- Actual query use: none. `QueryService._reader()` calls status without verification and checks `can_use_snapshot`, not `can_search` (`indexing/query.py:35-37`). Query latency is already fast because reads come from SQLite.
- Implemented search/export/context features: absent (`cli/app.py:120-173` reports them false), so fast does not currently unlock a real high-value downstream operation.

### Content-change question

- Can fast observe that a clean tracked path became dirty? Often yes, through status change.
- Can it distinguish two different contents that produce the same path/status projection? No.
- Can it safely prove Project KB's relevant content equality without hashing/otherwise content-verifying those objects? Not as implemented. It can only delegate a narrower claim to Git status, and the current delegated observer is demonstrably non-equivalent to publication.

### Terminology boundary

- **Fast verification**: the current cheap post-publication Git status/candidate proof. Recommendation: retire as an affirmative currentness mode.
- **Incremental refresh**: a future algorithm that builds a new complete snapshot by copying verified unchanged facts and re-extracting changed facts. Not implemented and not part of Stage 6 closure.
- **Fast query**: indexed reads from an already selected SQLite generation. Already valuable and independent of verification/refresh speed.

Recommendation: preserve fast query, defer incremental refresh, remove affirmative fast verification. Confidence: high.

## 6. Route comparison: A/B/C/D

### Route A — retain fast and strong, add dual-observer proof

- Summary: version and persist both live-index and neutral-index observer protocols; compare like with like.
- Benefits: preserves a roughly two-second status mode and existing public shape.
- Correctness: can fix the proven false `STALE` only if protocol provenance, Git configuration, EOL semantics, staged state, visibility, and observer parity are completely specified. It still does not compare Project KB object content.
- False-`CURRENT` risk: remains tied to Git status projection, stat/racy semantics, untracked/ignored scope, concurrent writers, and protocol implementation.
- False-`STALE` risk: Windows EOL, Git config, sparse/split/worktree variations, or future protocol drift can recur.
- Windows/Git complexity: highest. Requires retaining and expanding temporary-index and Git-storage safety logic.
- Test burden: highest; requires real Windows EOL, config, linked-worktree, split-index, visibility, and ABA matrices for two observers.
- Latency: best measured verification, but only 1.3–2.6 seconds faster than strong/full capture in dogfood.
- Maintenance/consumer clarity: poor; two meanings of `CURRENT` for one high-trust product.
- Reversibility: moderate, but new proof fields/contracts risk another compatibility burden.
- Approval needs: new public proof semantics and likely contract-version decisions.
- Decision: dominated by B/C for the stated product goal.

### Route B — strong-only authoritative verification

- Summary: fast never returns `CURRENT`; strong is the only currentness verification mode.
- Benefits: one high-trust proof path, existing semantic-v2 proof reused, seconds-scale latency, minimal compatibility change.
- Correctness: strongest current implementation within explicit exclusions; detects same-status content changes and checks binding/candidates/objects repeatedly.
- False-`CURRENT` risk: bounded to exclusions, concurrent-writer assumptions, safe-read limits, and bugs in the strong path.
- False-`STALE` risk: much lower because raw status is diagnostic; object/candidate/HEAD deltas drive stale decisions.
- Windows/Git complexity: fast-only branches disappear, but current strong still uses the neutral-index subsystem.
- Test burden: remove fast tests; retain/adapt strong, publication, binding, no-target, and no-live-Git-mutation tests.
- Latency: 3.4–4.5 seconds in evidence, acceptable relative to long tasks.
- Consumer clarity: good if output means `CURRENT_AT_VERIFIED_TIME`, not permanent currentness.
- Reversibility: high; current v2 stays valid.
- Approval needs: public CLI/deprecation and truth-label decisions.
- Decision: recommended Stage 6 closure.

### Route C — capture-first lifecycle

- Summary: when current structural facts are needed, stop writes and publish a new immutable full generation; query that generation. Strong remains optional diagnostic.
- Benefits: aligns data acquisition with task boundaries, makes query selection explicit, preserves baseline/working comparisons, and removes routine need to infer currentness from a stale snapshot.
- Correctness: full capture already reads/hashes/extracts the intended facts and validates stability before publication.
- Redundancy: immediate strong verification after a successful stable full capture repeats much of the same work and should not be mandatory.
- Truth claim: `CAPTURED_STABLE`; it does not promise no writes after capture.
- Concurrent writers: writes must be paused by workflow; capture detects many changes but cannot be absolutely linearizable without coordinated locking.
- Cost: requires new storage, identity, pointer, permissions, cleanup, recovery, selection, and comparison layers.
- Reversibility: high with immutable files and additive registry schema.
- Approval needs: substantial architecture and lifecycle decisions.
- Decision: recommended separate next Stage, not folded into Stage 6 closure.

### Route D — coordinated workspace lock

- Summary: an external lock/lease coordinates Project KB capture/verification with all repository writers.
- Benefit: can define a stronger linearization interval if every writer honors the same lock and crash/lease protocol.
- Risk: Git, editors, users, other agents, hooks, and arbitrary filesystem tools do not automatically honor it. A Project KB-only lock gives a false sense of coordination.
- Operational burden: lock ownership, stale leases, process death, cross-platform semantics, worktree scope, user override, and deadlock recovery.
- Necessity now: low. Manual write pauses plus stable full capture meet the stated first-stage goal.
- Approval needs: explicit cross-tool workflow policy.
- Decision: defer; reconsider only if empirical concurrent-writer failures justify it.

### Route recommendation

Recommended: **B now, C next**. Reject A as cost without product value and defer D. Main failure mode is accidental scope fusion: deleting current strong dependencies or implementing task lifecycle inside the Stage 6 fix. Confidence: high.

## 7. Recommended truthful snapshot semantics

### Publication proves

A successfully published current v2 snapshot proves, within declared exclusions:

- one sealed, internally valid SQLite database with matching run/snapshot identity and compatible structural contracts;
- facts produced from a bounded candidate population using safe reads/hashes/extraction;
- matching before/after/final repository state and object evidence at the checks performed by `IndexService.index()`;
- matching active repository binding at the pre-publication and post-publication reconciliation checks;
- atomic replacement of the canonical pathname, with the previous canonical preserved on pre-publication failure.

Recommended public claim: **`CAPTURED_STABLE`**.

`VALID_AT_CAPTURE` is acceptable explanatory text, but `CAPTURED_STABLE` better communicates that stability was checked over a bounded capture procedure rather than at an absolutely linearized instant.

### Publication does not prove

- That no write occurred after the final evidence check and before/during `os.replace`.
- That no write occurred immediately after publication.
- Permanent currentness.
- Content equality for hard-secret, ignored, or pruned exclusions.
- Absolute linearizability against uncooperative writers.

### Strong verification proves

Strong proves that, over a bounded stable verification procedure, the selected snapshot's candidate/HEAD/object proof and active binding match the observed repository within the declared scope. Recommended public interpretation: **`CURRENT_AT_VERIFIED_TIME`**, with `verified_at`, scope, exclusions, and attempts.

For wire compatibility, Stage 6 may retain enum value `CURRENT` while adding/documenting `truth_claim=CURRENT_AT_VERIFIED_TIME`. A later public-contract version can rename the value explicitly.

### Concurrent-writer assumptions

- Ordinary writes are expected to be paused during full lifecycle capture.
- Existing repeated observations and safe-read identity checks detect many concurrent changes and fail closed.
- A writer can always act after the last check; without an external coordination protocol, no result remains current forever.
- Absolute linearizability is possible only if every relevant writer honors one lock/transaction boundary or the filesystem provides a suitable repository-wide snapshot primitive. Neither exists here today.

Comparison:

| Label | Use |
|---|---|
| `CURRENT` | Too easily read as permanent; retain only as compatibility value with bounded semantics. |
| `CAPTURED_STABLE` | Recommended publication/lifecycle generation claim. |
| `VALID_AT_CAPTURE` | Accurate secondary description but less explicit about the bounded stability procedure. |
| `CURRENT_AT_VERIFIED_TIME` | Recommended strong-verification claim. |

Confidence: high.

## 8. Stage 6 simplification map

### Retain

- Full source capture and stability checks in `IndexService.index()`.
- Scanner safe reads, content hashes, hard-secret/pruned exclusions, no-target-execution boundary.
- Semantic-v2 schema, canonical module/source-root identity, snapshot-bound occurrences, logical keys, logical/observation fingerprints.
- Proof manifest and `PROOF_CONTRACT_VERSION=3`.
- Repository identity hash and binding generations.
- Version-first v1/v2 validation, cross-table invariants, query-time active-binding rechecks.
- Sealed temp snapshot, atomic publication, previous-snapshot preservation, quarantine/reconciliation.
- Strong object verification and its states `STALE`, `UNVERIFIED`, `CHANGED_DURING_CHECK`, and `ERROR`.
- Fast SQLite query behavior.

### Remove in Stage 6 closure

- Executable fast affirmative proof and its `CURRENT` result.
- Status-fingerprint inequality as a semantic `STALE` predicate (`snapshot/currentness.py:383-402`).
- Fast clean-status predicate (`currentness.py:404-418`).
- Fast-only third seal (`currentness.py:252-279`) once the fast execution branch is gone.
- Fast timing-map expectations, fast-specific recommended reindex path, and docs claiming two currentness quality levels.
- Fast-specific tests listed by the test auditor, including direct fast stale/current/index-flag/ABA/identity cases.

### Deprecate temporarily

- Accept the literal CLI token `--verify fast` for one compatibility window only to return a machine-readable `FAST_VERIFICATION_REMOVED`/`FAST_VERIFICATION_DEPRECATED` error with `pkb status ... --verify strong --json` as the action.
- Do not silently redirect to strong: it changes latency and semantics and makes automation unable to tell that fast was removed.
- Keep persisted v2 status/observation fields as validated legacy/provenance fields; stop using raw status inequality as content truth.
- Keep wire value `CURRENT` only if needed for contract compatibility, explicitly label it point-in-time.

### Do not remove in the minimum closure

Current strong verification calls `_capture_observation()` five times, which calls `capture_repo_observation()`, `temporary_git_index()`, and `live_git_index_generation()`. Therefore these are **currently shared strong dependencies**, not fast-only code:

- temporary visibility-neutral index;
- temporary `update-index` write capability;
- split-index-specific isolation and safety checks;
- live-index generation sealing;
- index-view plumbing in bounded Git wrappers;
- temporary cleanup/no-live-Git-mutation tests.

They may be removable later if strong is redesigned around live candidate/state observations plus object proof, but that change needs its own review and race/Windows/worktree validation. Removing them in the minimal fast retirement would violate the instruction not to delete mechanisms still required by strong.

### Tests that remain

- Safe scanner reads and same-status publication blocking (`tests/test_indexing_fixloop.py:144-603`).
- Sealed DB, exact run/snapshot relation, atomic replace, post-replace reconciliation/quarantine (`test_indexing_fixloop.py:639-1046`).
- V1 compatibility, v2 proof/invariant corruption, binding/relink/query rechecks, failed cutover preservation (`tests/test_stage6_snapshot_trust.py`).
- All canonical identity/module tests (`tests/test_stage6_identity.py`).
- Strong mutation matrix, same-status content changes, exclusions, terminal candidate/HEAD/content races, relink/identity races, and error-not-stale tests (`tests/test_stage6_currentness.py`).
- Canonical Git environment/allowlist, hard-secret exclusion, no target execution, and mechanism-neutral no-live-Git-mutation/worktree tests (`tests/test_stage6_safety.py`).

### Tests obsolete or adapted

- Remove fast-only behavior tests.
- Replace them with explicit old-CLI-token behavior tests.
- Retain temporary/split/index-generation tests while strong uses that implementation; retire them only in the same slice that replaces the strong observer.
- Rewrite brittle tests that assert exact private call counts to assert observable fail-closed outcomes.
- Add the real Windows CRLF regression absent today.

### CLI, state, gating, and docs

- CLI: public help becomes `--verify strong`; old fast token fails explicitly.
- Status: unverified published snapshot should expose `truth_claim=CAPTURED_STABLE`; successful strong exposes `CURRENT_AT_VERIFIED_TIME` plus time/scope/exclusions.
- Actions: a removed-fast invocation recommends strong, never reindex. Concrete strong object/candidate/HEAD `STALE` may still recommend a full capture/reindex.
- Gating: during Stage 6, current-only gates may continue to require successful strong verification. In the lifecycle Stage, replace permanent-current gating with the selected immutable generation and task write-epoch/lifecycle state.
- Durable claims requiring change after approval: `README.md:34-50`; `docs/DATA_SAFETY_POLICY.md:109-129`; `docs/DEVELOPMENT_CHECKLIST.md:55-70,90-93`; `docs/PROJECT_CONTEXT.md:37-43,69-75`; `docs/PROJECT_MAP.md:47-68,136+`.

### Minimum closure recommendation record

- Evidence: shared call path at `snapshot/currentness.py:184,225,292,424,449,614` and `indexing/scanner.py:142-212`.
- Alternative: remove the whole neutral observer now.
- Trade-off: retaining it carries complexity, but avoids an unapproved rewrite of the only high-trust verifier.
- Failure mode: premature removal weakens or breaks strong stability/visibility behavior.
- Confidence: high.

## 9. Compatibility and migration assessment

### Legacy v1

- Remains version-first recognizable and bounded-queryable (`validate_snapshot()`, `snapshot/database.py:733-852`).
- Remains incompatible with v2 currentness/proof operations.
- No in-place row migration.
- A lifecycle task baseline requires a fresh full v2 capture; this is a product capture, not a migration of the old database.

### Current semantic-v2 snapshots

- Remain structurally valid, queryable, repository-bound, and strongly verifiable.
- Fast removal does not alter stored structural facts or proof-manifest semantics.
- No schema change is required.
- `git_status_fingerprint`, `repo_state_*_json.status_fingerprint`, and observer fingerprints can remain harmless validated provenance. Removing columns would force a needless schema change.

### Current external v2 snapshot

Snapshot `b03ed877477c4445b0347e7695e98402` is compatible and freshly strong-verified. Keep it available. Do not reindex merely to remove fast. When generation storage arrives, Command Center may explicitly adopt/copy its immutable bytes as an initial project baseline after validation; do not mutate the canonical DB in place or silently promote it.

### Registry

- Current registry v3 remains valid for Stage 6 closure.
- The lifecycle Stage needs additive project/workspace/task/generation/pointer state. That is likely registry v4 or a dedicated control DB and requires separate approval/migration tests.
- Existing binding generations remain useful inputs but are insufficient as the complete workspace/task identity model.

### Contract versions

- Keep schema 2, proof 3, verifier 3 for the minimal closure because the stored proof remains consumable by the retained strong verifier.
- Do not bump a version merely because a public mode is removed; current exact compatibility checks would otherwise require every v2 snapshot to rebuild (`snapshot/database.py:924-962`).
- If a later strong-observer rewrite changes the meaning of stored proof fields, define explicit backward acceptance or a new contract then. Do not conflate that with fast retirement.

### Old CLI calls

Recommended behavior:

```text
pkb status <project> --verify fast --json
=> non-success FAST_VERIFICATION_REMOVED
=> snapshot remains AVAILABLE/COMPATIBLE
=> recommended action: pkb status <project> --verify strong --json
=> no REINDEX action
```

Do not silently redirect; do not classify the snapshot stale.

### Reindex answer

- Stage 6 closure: **no reindex required**.
- Lifecycle adoption: a user-controlled full task baseline capture is expected by product policy, but existing v2 bytes can remain queryable and can be explicitly adopted as a project baseline where permitted.
- Reindex is required only for v1-to-v2 use, actual proof incompatibility, wrong repository binding, corruption, or an approved future semantic contract change.

Confidence: high.

## 10. Task snapshot storage-model comparison

| Model | Atomicity/failure preservation | Windows locking | Comparison/query | Concurrency/isolation | Cleanup/growth | Migration/fit | Assessment |
|---|---|---|---|---|---|---|---|
| 1. One mutable SQLite per project | One DB transaction can be atomic, but failed/corrupt mutations share the canonical blast radius and old generations are overwritten. | Long/open readers can block replacement or writes. | Simple current query; historical comparison requires retained rows/versioning anyway. | Poor for tasks/worktrees; one mutable authority. | Compact but history absent or unbounded inside one file. | Closest to current path, but does not meet immutable lineage. | Reject. |
| 2. `baseline.sqlite` + `working.sqlite` mutable | Preserves one baseline but every working refresh overwrites recovery/comparison evidence. Two pointers are encoded as filenames. | Same replace/open-file issues; old working readers race replacement. | Easy baseline/latest compare only. | Cannot represent parallel tasks, fix-loop history, or multiple worktrees safely. | Bounded but destructive; no generation recovery. | Superficially simple but insufficient. | Reject. |
| 3. One SQLite with multiple generations | Best single-DB transaction for generation rows and pointers; failed transactions roll back. Corruption/migration affects all generations. | Writer/read contention and file-size/migration pressure; WAL/locking policy becomes product architecture. | Easiest SQL joins across generations. | Logical isolation possible, physical blast radius shared. | Vacuum/retention complicated; file grows. | Large rewrite of current one-snapshot schema and validator. | Viable alternative, not preferred. |
| 4. Immutable SQLite per generation + managed pointers | New file is built/sealed/validated, then pointer transaction publishes it. Crash between file and pointer leaves a recoverable orphan; pointer never targets a partially built file. | Readers never require replacing an open generation; deletion can be deferred if locked. | Open/attach two immutable DBs or compare in service; selection adds one pointer lookup. | Natural task/workspace isolation; immutable sharing is safe. | Explicit files make size/count policy and orphan detection straightforward. | Reuses current build/validate/publish foundation with additive control metadata. | **Recommend.** |

### Model 4 recommendation record

- Evidence: current writer already builds a complete sealed temp SQLite then atomically publishes; current reader opens SQLite read-only per query (`snapshot/database.py:1730-1745`).
- Alternatives: Model 3 has simpler cross-generation SQL but a larger corruption/locking/migration blast radius; Model 2 cannot represent lineage.
- Trade-off: pointer/recovery logic and cross-file comparison are more complex than a single mutable database.
- Failure modes: orphan final file after crash, pointer row without file if ordering is wrong, locked-file cleanup, duplicate generation publication. Section 11 defines the safe order.
- Confidence: medium-high pending control-database decision.

## 11. Recommended generation and pointer model

### Physical layout

Conceptual layout, not approved implementation:

```text
projects/<project_id>/
  generations/<snapshot_id>.sqlite       # immutable, create-once
  runs/<run_id>.json                     # ancillary evidence
  tmp/<capture_id>.tmp.sqlite             # never queryable
```

Do not encode baseline/working authority in mutable filenames. Roles and selected generations belong in managed metadata.

### Control metadata

Minimum conceptual tables/entities:

- `projects`: logical repository/product identity.
- `workspaces`: one checkout/worktree binding and binding generation.
- `tasks`: task lineage, state, owner workspace, baseline generation, timestamps.
- `snapshot_generations`: snapshot ID, project/workspace/task, role, sequence, path, proof versions, capture state, immutable byte hash/size, parent generation where useful.
- `pointers`: project baseline, task baseline, task latest working, optional selected generation; updates use compare-and-swap/expected predecessor.
- `operation_intents`: idempotency token and state for begin/capture/promote/close/recovery.

### Publication protocol

1. Validate active project/workspace/task binding and authorization.
2. Pause repository writes for capture.
3. Build in a managed temp path on the destination volume.
4. Seal and validate the database; close all SQLite handles.
5. Atomically move to a create-once immutable `generations/<snapshot_id>.sqlite`. Never replace an existing generation.
6. In one control-DB transaction, insert the generation row and compare-and-swap the appropriate pointer/sequence.
7. Return success only after pointer transaction commits.

Failure behavior:

- Failure before step 5: previous pointers/generations unchanged; temp is removable/recoverable.
- Crash after step 5 before step 6: validated unreferenced orphan; recovery may attach it only when intent, ownership, binding, and expected predecessor all match, otherwise quarantine/delete with user policy.
- Failure in step 6: no pointer movement; immutable file remains orphaned for recovery.
- A pointer must never be written before the immutable file is validated and present.
- An existing destination ID is idempotent success only if byte hash and all ownership metadata match; otherwise it is a collision/error.

### Pointer semantics

- `project_baseline`: user-promoted stable project reference.
- `task_baseline`: immutable first full capture for one task; never moves.
- `task_latest_working`: monotonically advances within one task using expected previous generation and sequence.
- `selected_generation`: prefer request/session selection rather than a shared mutable pointer for Codex queries. A durable default, if needed, is user-controlled.

The filesystem publication and registry transaction cannot be one physical transaction. The safe ordering deliberately permits orphan files but never a pointer to incomplete/missing bytes.

## 12. Identity, workspace, and task ownership

### Minimum persisted identity

| Identity | Recommended meaning |
|---|---|
| `project_id` | Stable logical Project KB project/repository family. |
| `workspace_id` | Stable generated ID for one checkout/worktree registration; not merely a path hash. |
| `workspace_binding_generation` | Rotates when the registered path/identity is relinked or replaced; adapts current `repo_binding_generation`. |
| `task_id` | UUID for one implementation/task lineage, scoped to project and workspace. |
| `snapshot_id` | Existing immutable v2 snapshot UUID; physical generation identity. |
| `snapshot_role` | `TASK_BASELINE`, `TASK_WORKING`, and a pointer role such as promoted `PROJECT_BASELINE`; promotion should not rewrite the file's embedded facts. |
| `generation_sequence` | Monotonic integer unique within a task; baseline is sequence 0 or kept separately, working begins at 1. |

### Fit with current model

- Retain current `project_id`, snapshot ID, repository identity hash, normalized root evidence, and binding-generation concepts.
- Split current one-row ownership: current `projects.repo_root_norm` conflates logical project and workspace. A linked worktree has a distinct top-level root but may share the Git common directory and logical history.
- Move checkout-specific root/fingerprint/binding to `workspaces`; keep logical repository anchors at project level where they truly identify the same project.
- Snapshot metadata should add workspace/task/role/sequence in a future schema/sidecar control record. Existing v2 bytes can be represented by control metadata without in-place mutation.
- Resolver ownership expands from project resolution to project + workspace + active/selected task resolution. Query service must receive an explicit selected generation.

### Required scenarios

- Main checkout: one workspace row bound to the main top-level root.
- Linked worktree: another workspace ID under the same logical project, with its own Git worktree/index path and binding generation.
- Two parallel write-capable Codex tasks: require two workspace/worktrees. Do not allow concurrent write-capable active tasks in one physical workspace.
- Same project, separate task lineages: separate `task_id`, baseline, working sequence, and pointers; shared immutable project baseline is read-only.
- Two tasks in one workspace: at most one may be `ACTIVE_WRITE`; others are paused/read-only. Otherwise a write cannot be attributed to one lineage.
- Relink: rotate only the affected workspace binding; pause its active tasks and require explicit recovery/rebind. Preserve old generations as queryable historical task facts but prohibit new captures under the old binding.
- Task restart: resolve by task ID and workspace binding, validate pointer/file/hash invariants, then resume without creating a new baseline.

High-risk interpretation: treating each worktree as a separate current `project_id` avoids schema work but fails the requested “same project, separate task lineages” model and complicates promotion to one project baseline. Confidence: high.

## 13. Lifecycle operations and permission model

| Operation | Actor / approval | Preconditions and role | Atomicity/state transition | Failure and idempotency | Pause repo writes? |
|---|---|---|---|---|---|
| Begin task | User/Command Center; explicit approval | Healthy project/workspace; no other active writer in same workspace. Creates `DRAFT` task with idempotency key. | One control transaction: absent -> `DRAFT`. | No task on rollback; repeat token returns same task. | Not for metadata-only begin; yes before baseline capture. |
| Capture baseline | User/Command Center; explicit approval | `DRAFT`, matching workspace binding, no baseline. Role `TASK_BASELINE`. | Full immutable publication + transaction sets baseline and task `ACTIVE`. | Old project baseline untouched; failed/orphan capture recoverable; token makes retry idempotent. | **Yes.** |
| Refresh working | Codex inside authorized active task; no per-refresh promotion approval | `ACTIVE`, baseline exists, matching binding, expected previous sequence. Role `TASK_WORKING`. | Full immutable publication + CAS advance of latest-working pointer/sequence. | Previous working pointer remains on failure; retry token cannot double-advance. | **Yes; Codex stops writes first.** |
| List generations | User or Codex | Read access to project/task. | Read-only. | Naturally idempotent. | No. |
| Select generation | Codex may select explicitly per query; user controls durable default | Generation belongs to permitted project/task/workspace and validates. | Prefer request/session parameter; durable default is one pointer transaction. | Invalid/missing generation leaves selection unchanged. | No. |
| Compare generations | User or Codex | Both generations authorized, compatible enough for requested comparison. | Read-only over immutable DBs. | Return bounded incompatibility, never mutate/guess lineage. | No. |
| Promote final | User/Command Center only; explicit approval | Active task; final full capture/selected working validates and binding matches. | Transaction moves project-baseline pointer and marks task `ACCEPTED`/final generation. File bytes unchanged. | Previous project baseline remains on rollback; idempotent if already pointing at same generation. | Yes for the final capture; not for pointer-only replay. |
| Close task | User/Command Center only; explicit approval | Accepted/promoted or explicit abandoned disposition. | Transaction marks `CLOSED`/`ABANDONED`, freezes pointers, enqueues cleanup policy. | Cleanup failure yields closed-with-pending-cleanup, not reopened or half-deleted. | No, unless closure includes final capture. |
| Clean working history | User/Command Center only; explicit deletion approval | Closed/abandoned task; generation is not any baseline/current pointer and ownership matches exactly. | Mark pending-delete transaction, delete/attempt delete, finalize metadata transaction. | Locked Windows files stay pending; never broaden by path prefix alone; repeat is idempotent. | No. |
| Recover interrupted task | User/Command Center for state-changing recovery; Codex may inspect | Valid task/workspace binding; operation intent and files/pointers inspected. | Reconcile temp/orphan/pointer states by bounded state machine. | Attach only exact validated intent-owned orphan; otherwise quarantine/pending cleanup. | Metadata inspection no; resumed capture yes. |

### Permission conclusion

The proposed permission model is sound with two corrections:

1. Codex may create a new working generation only inside an explicitly active task grant and only after it stops repository writes.
2. Codex should query by explicit generation ID instead of changing a durable shared selection pointer unless the user delegated that pointer operation.

Codex cannot establish/promote project baseline, close/abandon a task, or delete history. Confidence: high.

## 14. Fix-loop/Stage lineage

### New `task_id`

Create a new task ID for:

- a new full implementation task with a separately accepted scope;
- a new Stage;
- work in another workspace/worktree;
- a restarted effort after explicit abandonment rather than recovery;
- a review finding that is accepted as a separate implementation task rather than a continuation of the active one.

### Reuse existing `task_id`

Reuse for:

- meaningful write batches in the same approved implementation scope;
- review-requested fixes and fix-loops before the task is accepted/closed;
- test/doc adjustments inside the same approved task boundary;
- restart/recovery of an interrupted active task with the same workspace binding.

### Baseline rules

- One task baseline is created once, before its first write batch.
- A fix-loop does not create a new baseline; it creates another working generation.
- Project baseline advances only through explicit user promotion of a final generation.
- A new Stage gets a new task and baseline even if it starts from the just-promoted project baseline.

### Review, commit, and closure

- Review-only activity can read/compare existing generations and does not inherently create a task or working generation.
- A commit does not close or promote a task. Working generations and task lineage survive Review and Commit.
- Acceptance + explicit promotion advances the project baseline.
- Explicit closure triggers retention policy; not commit, merge, or elapsed time.
- Abandoned tasks remain isolated until the user explicitly closes/cleans them; no automatic promotion.

### Parallelism rule

Two write-capable tasks cannot safely share one worktree concurrently because repository writes have no task label. Use separate linked worktrees/workspace IDs or serialize by pausing one task. This is a high-confidence ownership constraint, not an optional optimization.

## 15. Snapshot comparison boundary

### Already possible with current schema v2

Deterministic exact-set comparisons can be built without heuristic lineage:

- Files: added/removed by `path_key`; content-changed by `content_hash`; classification, size, encoding, analysis level, and parse-state changes from `files`.
- Parse diagnostics: exact tuple differences by file path, kind, message, line, and column.
- Module identity: source root, module name, resolution status/candidates, importability fields.
- Symbols: exact occurrence-signature set differences using joined file `path_key` plus kind, binding role, coordinates, ordinal, signature, names, and extractor version.
- Imports: exact occurrence tuple differences using file path, import kind/text/name/alias/level/range and stored resolution fields.
- Relations: exact tuple differences using relation kind, source/target occurrence facts/text/ranges/status/evidence.

### Identity caveat

Current `occurrence_id()` deliberately includes `snapshot_id` and promises no cross-snapshot identity (`indexing/identity.py:20-47`). Therefore raw v2 `symbol_id`, file ID, import ID, and relation ID cannot be equated across generations.

Minimum comparison should:

- report exact added/removed occurrence signatures;
- use `logical_key` only as a deterministic non-lineage grouping when unique;
- classify “changed” as removed+added unless an unambiguous deterministic grouping exists;
- report ambiguity instead of pairing duplicate logical keys.

### Requires future structural-semantics work

- Rename/move lineage.
- Stable entity lineage across large edits or coordinate shifts.
- Semantic equivalence of signatures/bodies.
- Heuristic matching of duplicate logical definitions.
- Behavior/call/reference graph changes beyond currently extracted exact relations.

Recommendation: first comparison API should be deterministic set difference only. Confidence: high.

## 16. Retention, cleanup, and recovery

### Compared policies

- Baseline + latest only: cheapest storage, but destroys fix-loop evidence and weakens recovery/comparison. Reject for the first lifecycle dogfood.
- All working generations until closure: simplest truthful lineage and recovery; bounded by task lifetime. Recommend initially.
- User/Codex-selected checkpoints: useful later, but adds checkpoint/deletion policy before value is proven.
- Final promoted baseline only: recommended **after explicit closure**, not during active work.

### Recommended bounded first policy

- While active: keep task baseline plus every successfully published working generation.
- Soft/hard bounds: Command Center must choose exact values; starting proposal is warning at 20 generations or 2 GiB per task, whichever comes first. Never auto-delete an active task generation to make room.
- At bound: require user-approved prune/closure or raise a bounded storage error; Codex cannot delete.
- On accepted closure: retain the promoted immutable generation as project baseline; remove non-promoted task working generations and the task baseline only according to explicit closure policy. Keep small task/generation metadata and hashes for recovery/audit if approved.
- On abandoned closure: user chooses retain-for-recovery or delete all task-owned generation files; never affect another task/workspace/project.

### Failed captures and orphans

- Failed pre-seal temp files: safe to clean after ownership/path validation; never query.
- Valid orphan generation after crash-before-pointer: recover using operation intent and exact ownership/predecessor checks, or mark for cleanup.
- Invalid/partial file: quarantine/delete only through controlled recovery.
- Pointer to missing file is a blocking invariant violation; normal write ordering must make it impossible.
- Windows locked deletion: mark `PENDING_DELETE`, leave bytes, retry later. Do not treat inability to delete as task-state rollback.
- Automatic cleanup is allowed only for definitely unreferenced temp/orphan files under managed ownership rules; task history cleanup waits for explicit closure/user approval.

### Safety key

Every cleanup predicate must include `project_id`, `workspace_id`, `task_id`, `snapshot_id`, expected managed root, role/pointer references, and immutable file identity/hash. Path-prefix matching alone is insufficient.

## 17. Future incremental-refresh compatibility

Incremental refresh stays outside the immediate work.

Required correctness law:

```text
incremental_snapshot(state X) == full_snapshot(state X)
```

Equality means the same logical snapshot fingerprint and equivalent structural/proof rows after ignoring allowed generation-specific IDs/timestamps.

Retain now:

- content hashes and bounded metadata evidence;
- candidate population and normalized path keys;
- scanner/policy/extractor/module/occurrence contract versions;
- logical structural fingerprint separate from observation fingerprint;
- snapshot-bound occurrences plus deterministic logical keys;
- immutable generations and explicit parent/sequence metadata;
- full-capture fallback.

Future algorithm requirements:

1. Detect changed paths through a trustworthy mechanism.
2. Copy forward facts only for unchanged objects whose proof and relevant policy/contracts match.
3. Re-read/re-hash/re-extract changed objects.
4. Recompute global module/import/relation invariants where local changes can have non-local effects.
5. Validate the complete new snapshot and proof manifest.
6. Fall back to full rebuild on uncertainty, incompatible policy/version, ambiguous global impact, or unstable repository.
7. Differentially test incremental versus full snapshot over edits, deletes, adds, staging/worktree states, module roots, parse failures, and Windows EOL cases.

Do not retain fast status-currentness solely as a future incremental detector; detection is not proof of semantic equivalence.

## 18. Validation and dogfood plan

### Blocking test-harness prerequisite

The test auditor found that helper subprocess environments copy all parent variables and do not remove `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, object-directory redirectors, or dynamic config controls. Production `safe_git_environment()` does remove inherited `GIT_*` variables (`git_utils.py:68-93`).

Before broad Stage validation, make test Git helpers use an equivalent deny-by-default scrub plus deterministic no-prompt/no-hook identity. This is a validation-safety change, not a product architecture decision.

The audit's focused pytest run began before this independent finding arrived. The process environment was then checked and contained no `GIT_*` entries, so the disposable fixtures were not redirected in this run. Do not generalize that safety to other environments.

### Stage 6 closure gates

- Full-capture publication and strong proof correctness, including same-status content mutation.
- Fast token cannot yield `CURRENT`, `STALE`, or reindex advice; it returns the approved explicit compatibility response.
- Existing v2 remains valid/queryable/strong-verifiable; v1 behavior unchanged.
- No regression in binding, relink quarantine, identity, atomic replace, failed refresh preservation, or query binding rechecks.
- Real Windows CRLF fixture: LF blobs + CRLF worktree through production sanitized Git boundary; full capture/strong truth remains correct and no status-only false classification is accepted.
- No target module execution, hard-secret content access, live-index/config/common-dir mutation, shared-index creation, or lingering temp files.
- Actual `os.replace` failure/locked-reader coverage on Windows where feasible.
- Durable docs changed separately through approved one-file docs-update tasks; do not leave dual-fast claims.

### Task Snapshot Lifecycle gates

- Immutable generation bytes and create-once IDs.
- Generation sequence monotonicity and CAS pointer update.
- Pointer never targets missing/unsealed/invalid generation.
- Failed refresh preserves previous latest pointer and all prior generation bytes.
- Project/workspace/task isolation, including main + linked worktree and two parallel tasks.
- One write-capable task per workspace.
- Relink pauses/invalidates only affected workspace binding.
- User-only promote/close/delete; Codex working-capture/query/compare permissions enforced.
- Idempotent begin/capture/refresh/promote/close/recover.
- Crash at every file/pointer boundary; orphan attach/quarantine; temp cleanup; locked-file deferred cleanup.
- Retention count/size behavior and no cross-task deletion.
- Baseline/working/final selection and deterministic comparison.

### Competitive product validation later

Compare:

- Codex using user baseline + immutable working generations;
- Codex native repository workflow without Project KB;
- action count and elapsed time;
- structural coverage and missed/incorrect facts;
- false confidence/currentness claims;
- practical usefulness of exact generation comparison.

Do not optimize refresh until this validates product value.

### Checks run in this audit

- `git status --porcelain=v2 --branch --untracked-files=all` and diff/path inventory.
- Permitted evidence presence and v6 diff path inventory.
- Read-only source/docs/test inspection and line/function inventories.
- Fresh `uv run --no-sync pkb status antique-attribution-ai --verify strong --json`: `CURRENT`, 4,503 ms, no deltas.
- Fresh corresponding fast check: false `STALE`, 1,955 ms, identical dogfood status hashes.
- Registry/snapshot SHA-256 before/after verification: unchanged.
- Environment check: no inherited `GIT_*` values.
- Focused pytest selection: 20 passed, 136 deselected in 74.87 s; one non-functional pytest-cache warning. No broad suite.

## 19. Independently reviewed implementation slices

No slice is approved. Each needs a fresh fixed-scope task and independent review.

### A. Stage 6 currentness simplification/closure

- Goal: strong-only authoritative verification; fast cannot return current/stale or advise reindex; truthful bounded labels.
- Principal files/layers: `cli/app.py`, `snapshot/currentness.py`, `resolver/{project,state,models}.py`, `gating/*`, directly related tests; durable docs through separately approved docs-update tasks.
- Schema effect: none; retain v2 fields/contracts.
- Compatibility effect: explicit old-fast CLI response; v1/v2 query compatibility unchanged.
- Tests: test-helper Git scrub first; retained strong/full/binding/publication tests; old-fast contract; Windows CRLF dogfood.
- Stop conditions: strong dependency would be removed, v2 exact compatibility would be bumped, or a reindex becomes necessary.
- Repo change handoff: one focused code/test diff, then separate docs update(s), then independent review.
- Gate: existing v2 strong-verifies; no fast affirmative result; no live-Git/DB mutation; focused tests/dogfood pass.

### B. Generation storage

- Goal: create-once immutable generation file publication and metadata registration without task semantics beyond ownership placeholders.
- Principal files/layers: `snapshot/database.py`, new/approved generation repository/service boundary, storage path owner, registry/control schema.
- Schema effect: additive control metadata; snapshot schema change only if embedded ownership is approved.
- Compatibility effect: canonical `kb.sqlite` remains readable during migration; no in-place mutation.
- Tests: immutable bytes, duplicate IDs, failed build/validation/rename, crash-created orphan, locked reader, previous snapshot preservation.
- Stop conditions: pointer can precede file validation, existing generation can be replaced, or Windows open-reader behavior is unresolved.
- Handoff: generation writer/reader only, no lifecycle commands.
- Gate: validated immutable file + recoverable orphan behavior.

### C. Task/workspace state and pointers

- Goal: additive project/workspace/task/generation identity and transactional pointers.
- Principal files/layers: registry models/schema/service, resolver models/service, storage ownership.
- Schema effect: registry v4 or approved dedicated control DB.
- Compatibility effect: migrate each current project to a main workspace; preserve project ID/storage/binding evidence.
- Tests: v3 migration, main/linked workspace, parallel tasks, relink, pointer CAS, task restart.
- Stop conditions: project/workspace identity is not approved, migration is destructive, or two writers can be active in one workspace.
- Handoff: state model and resolver, no lifecycle mutations beyond internal fixtures.
- Gate: isolation/invariants/migration idempotency.

### D. Lifecycle operations

- Goal: begin, baseline, refresh, promote, close, and recover with permissions/idempotency.
- Principal files/layers: new approved lifecycle application service, CLI/Command Center adapter, authorization/gating, generation/control repositories.
- Schema effect: operation intents and task state transitions.
- Compatibility effect: current `index` retained/deprecated according to approved command migration.
- Tests: every transition, actor denial, idempotent retries, failure at file/pointer boundaries.
- Stop conditions: Codex can promote/delete, capture occurs while writes are not paused by workflow, or action combines unrelated transitions.
- Handoff: one transition family per reviewed change where practical.
- Gate: permission and atomicity matrices pass.

### E. Snapshot selection/comparison

- Goal: query explicit generation and provide deterministic exact-set comparisons.
- Principal files/layers: `indexing/query.py`, `SnapshotReader`, new comparison service/output contract.
- Schema effect: none initially if control metadata supplies generation ownership.
- Compatibility effect: default canonical query remains available until selection migration is approved.
- Tests: cross-task access denial, incompatible versions, exact file/fact diffs, duplicate logical-key ambiguity, no heuristic lineage.
- Stop conditions: raw snapshot-bound IDs are treated as cross-snapshot identity or comparisons guess renames.
- Handoff: selection first, then bounded comparisons.
- Gate: deterministic repeatable output on immutable pairs.

### F. Cleanup/recovery

- Goal: safe bounded retention, orphan/temp recovery, deferred Windows deletion.
- Principal files/layers: lifecycle/control repository and storage owner only.
- Schema effect: pending-delete/quarantine/operation-intent states.
- Compatibility effect: none to snapshot semantics.
- Tests: locked files, cross-task/path attacks, pointer protection, crash replay, count/size limits, idempotent cleanup.
- Stop conditions: active/referenced generation can be deleted or cleanup scope is inferred only from paths.
- Handoff: recovery before destructive cleanup.
- Gate: zero cross-owner deletion under fault injection.

### G. Dogfood

- Goal: run the approved lifecycle on real Windows projects/tasks without new semantic features.
- Principal files/layers: approved wrapper/evidence procedure only; no product mutation during audit.
- Schema effect: none beyond implemented slices.
- Compatibility effect: validate old v2 and newly captured generations.
- Tests/evidence: before/after source and Git-storage oracles, baseline/working/final IDs, timings, comparisons, failure/restart case.
- Stop conditions: target/registry mutation outside approved commands, secret/target execution, network, or unexplained pointer/storage drift.
- Handoff: preserved dogfood report, then independent review.
- Gate: trustworthy generation selection, isolation, recovery, and useful comparisons with bounded action/time cost.

## 20. Decisions required from Command Center

The report recommends but cannot approve:

1. Approve `B -> C`: strong-only Stage 6 closure followed by a separate capture-first lifecycle Stage.
2. Approve explicit failure/deprecation behavior for old `--verify fast`; recommendation is machine-readable failure with strong action, not silent redirect.
3. Approve bounded public truth labels: `CAPTURED_STABLE` and `CURRENT_AT_VERIFIED_TIME`, including whether wire enum `CURRENT` is temporarily retained.
4. Approve keeping temporary-index/index-generation/split-index machinery in the minimum closure because strong still depends on it, with any removal deferred to a separate strong-observer slice.
5. Approve Model 4 immutable generation files plus transactional managed pointers.
6. Choose control metadata location: registry v4 versus a dedicated project-control database. Recommendation leans registry/control DB with one transaction owner, but this needs an explicit architecture decision.
7. Approve splitting logical `project_id` from `workspace_id` and allowing only one write-capable active task per workspace.
8. Approve role/state/permission model, especially Codex working-capture authority and user-only promotion/closure/deletion.
9. Choose exact retention limits and accepted/abandoned closure cleanup policy; starting proposal is warning at 20 generations or 2 GiB and no active-task auto-deletion.
10. Decide whether an existing validated v2 canonical may be explicitly adopted/copied as an initial project baseline or whether every lifecycle task always requires a fresh baseline capture.
11. Approve additive registry migration scope and CLI migration from the current one-canonical `index` model.
12. Approve test-helper Git environment sanitization as a prerequisite validation fix.

No implementation should start until at least decisions 1–8 and 12 are fixed for the relevant slice.

## 21. Risks and unresolved questions

### Blocking

- Test-helper Git subprocess environments are not deny-by-default. This blocks safe broad validation in contaminated environments.

### High

- Strong still carries the neutral-index machinery. Keeping it is safe but leaves maintenance cost; removing it requires a separate proof that live observation + object proof preserves fail-closed behavior.
- Current logical project/workspace conflation cannot represent linked worktrees and parallel tasks cleanly.
- File publication and pointer transaction are separate durability domains; recovery must make orphan files first-class and forbid pointer-first ordering.
- Windows file locking can delay cleanup; state must represent pending deletion.
- The workflow pause is cooperative and does not stop arbitrary writers. Truth labels must remain bounded.
- An unbounded active task can consume storage if all generations are retained; hard behavior at the limit requires Command Center choice.

### Medium

- Current query context exposes currentness fields even though queries do not verify currentness; lifecycle selection should make generation identity primary.
- Several current tests assert private call counts and will impede safe observer simplification.
- Current `observation_fingerprint` incorporates repository-state fields that become legacy provenance after fast removal; keep for v2 compatibility but avoid granting new semantics.
- Cross-generation comparison must not mistake snapshot-bound IDs or duplicate logical keys for lineage.

### Unresolved questions

- Should project identity be derived from common Git identity or remain an explicit user-created logical project that may own several workspaces?
- Should the project baseline point directly to a task working generation or to a byte-identical promoted copy? Direct pointer is simpler and preserves immutability; policy must approve it.
- Are closed task metadata and hashes retained indefinitely, or removed with files? This is governance, not required for structural correctness.
- Is a durable “selected generation” pointer needed at all, or are explicit query parameters/session state sufficient?
- When a workspace relinks to the same logical repository, can an active task be explicitly rebound after strong validation, or must it close/restart? Safest first version pauses and requires user recovery.
- What exact durability guarantees (file flush/directory flush) are required beyond current SQLite close + atomic rename on supported Windows filesystems?

## 22. Explicit non-approval boundary

This report:

- recommends a currentness route and lifecycle architecture for Command Center decision;
- does not approve either direction;
- does not authorize code, test, config, registry, schema, CLI, durable-doc, task, Stage Brief, plan, or workflow changes;
- does not approve deletion of fast/temporary-index code or tests;
- does not approve a schema/contract bump or reindex;
- does not approve another real index, migration, promotion, cleanup, commit, merge, release, or deployment.

The only repository file created by this audit is this report.
