# Stage 6 Trust Substrate Closure Report

Boundary: this report is preserved analysis, not source of truth, and does not authorize implementation, commit, merge, release, deployment, Stage transition, or Stage 7 work.

## 1. Metadata and inspected boundary

- Task: Stage 6 Closure, Slice 6.4C — Strong-Only Dogfood and Stage Closure.
- Initial task envelope: `docs/codex/tasks/stage-6-closure-slice-6-4c-strong-dogfood.md`.
- Approved downstream scope: `docs/codex/stage_briefs/stage-6-trust-substrate-closure-brief.md`.
- Route: L3 Independent Checks, resolved to `docs/codex/reasoning/L3_self_consistency.md`.
- Skills: `reasoning-patterns`, `audit-only`, and `report-writing`.
- Agents: none; the deterministic checks did not benefit materially from a subagent.
- Intent: read-only closure validation against one already-published real semantic-v2 snapshot; report-only.
- Inspected: bounded Project KB Git state, registered project row, canonical snapshot metadata, target Git identity/status/storage, public status behavior, five retained strong samples, mutation manifests, full pytest/static/CLI gates.
- Not inspected or executed: target code/tests/scripts/services, network/fetch, a new index, superseded Stage 6 artifacts, Stage 7 behavior, Git object-database recursive hashes.

## 2. Architecture and slice preconditions

Facts:

- The approved contract is `CAPTURED_STABLE + UNVERIFIED` after publication without verification and `CURRENT_AT_VERIFIED_TIME + CURRENT` only after strong verification.
- The legacy `fast` token must fail explicitly and must not redirect to strong.
- Existing semantic-v2 snapshots must remain valid without schema migration, reindex, or in-place mutation.
- Slice 6.4C is report-only and permits no implementation, test, durable-documentation, registry, snapshot, or target-repository change.

Interpretation: L3 Independent Checks remained the lightest sufficient route after reading the closure brief. No ExecPlan or subagent was required.

## 3. Project-kb and target base state

| Repository | HEAD | Branch | Staging | Worktree |
| --- | --- | --- | --- | --- |
| `project-kb` | `5af6736257f95bb958534b7cc74525200d81dc01` | `main` | empty | expected uncommitted semantic-v2/6.4A/6.4B surface; no unrelated path detected |
| `antique-attribution-ai` | `443a7a42478a09ad6a61a364b6ea817e44cb97ca` | `main` | empty | clean |

The registered target root was `<TARGET_REPO>`. Its repository root, Git directory, and Git common directory all resolved to the expected normal-checkout paths; the real index resolved to `<TARGET_REPO>\.git\index`.

## 4. Existing semantic-v2 snapshot identity

Read-only SQLite inspection used URI `mode=ro&immutable=1`.

| Field | Value |
| --- | --- |
| Project ID | `<PROJECT_ID>` |
| Storage | `<PROJECT_KB_HOME>\projects\<PROJECT_ID>` |
| Canonical snapshot | `...\kb.sqlite` |
| Snapshot ID | `b03ed877477c4445b0347e7695e98402` |
| Schema / build | semantic v2 / `SEALED` |
| Snapshot Git HEAD / branch | `443a7a42478a09ad6a61a364b6ea817e44cb97ca` / `main` |
| Created / registry last indexed | `2026-07-15T01:02:57.773341Z` / exact match |
| Published run | `3bec1274f1e543fe9a27ddc6789c33ca`, the only `index_runs` row |
| Proof / verifier contract | `3` / `3` |
| Availability / compatibility | `AVAILABLE` / `COMPATIBLE` |

Fact: the registry timestamp, snapshot timestamp, published run, and expected snapshot ID align. No later index was present or required.

## 5. Normal CAPTURED_STABLE status result

Command: `pkb status antique-attribution-ai --json`

Result: exit `0`, stderr empty, `ok=true`, code `SNAPSHOT_PRESENT_UNVERIFIED`.

Exact public `snapshot_check` contract returned:

```json
{"status":"valid_when_published_currentness_unverified","snapshot_id":"b03ed877477c4445b0347e7695e98402","indexed_at":"2026-07-15T01:02:57.773341Z","git_commit_at_index":"443a7a42478a09ad6a61a364b6ea817e44cb97ca","current_git_commit":null,"reason":"present_working_tree_not_compared","availability":"AVAILABLE","compatibility":"COMPATIBLE","currentness":"UNVERIFIED","truth_claim":"CAPTURED_STABLE","verification_mode":null,"verified_at":null,"verification_duration_ms":null,"verification_timings_ms":{},"verification_attempts":0,"mismatch_paths":[],"deltas":[],"diagnostics":[],"exclusions":[],"verification_scope":{},"proof_contract_version":null,"verifier_version":null}
```

Exact stdout response envelope (single JSON line):

    {"ok": true, "result": "success", "code": "SNAPSHOT_PRESENT_UNVERIFIED", "command": "status", "message": "A valid published snapshot is available; present working-tree currentness is unverified.", "data": {"resolution": {"mode": "name", "status": "resolved", "project_name_input": "antique-attribution-ai", "working_directory": "<PROJECT_KB_REPO>", "resolved_repo_root": "<TARGET_REPO>", "resolved_by": "project_name"}, "project": {"project_id": "<PROJECT_ID>", "project_name": "antique-attribution-ai", "repo_root": "<TARGET_REPO>", "storage_path": "<PROJECT_KB_HOME>\\projects\\<PROJECT_ID>", "created_at": "2026-07-13T03:34:17Z", "last_status": "INDEX_SUCCEEDED", "last_indexed_at": "2026-07-15T01:02:57.773341Z", "last_git_commit": "443a7a42478a09ad6a61a364b6ea817e44cb97ca", "updated_at": "2026-07-15T01:02:58.835299Z"}, "project_state": "SNAPSHOT_PRESENT_UNVERIFIED", "repo_check": {"status": "valid", "stored_repo_root": "<TARGET_REPO>", "current_git_root": "<TARGET_REPO>", "path_exists": true, "is_directory": true, "is_git_repository": true, "git_root_matches": true, "fingerprint_status": "matched", "fingerprint_strength": "strong", "reason": "strong_fingerprint_matched"}, "storage_check": {"status": "valid", "expected_storage_path": "<PROJECT_KB_HOME>\\projects\\<PROJECT_ID>", "actual_storage_path": "<PROJECT_KB_HOME>\\projects\\<PROJECT_ID>", "storage_exists": true, "is_directory": true, "exports_exists": true, "runs_exists": true, "missing_paths": [], "reason": "storage_valid"}, "snapshot_check": {"status": "valid_when_published_currentness_unverified", "snapshot_id": "b03ed877477c4445b0347e7695e98402", "indexed_at": "2026-07-15T01:02:57.773341Z", "git_commit_at_index": "443a7a42478a09ad6a61a364b6ea817e44cb97ca", "current_git_commit": null, "reason": "present_working_tree_not_compared", "availability": "AVAILABLE", "compatibility": "COMPATIBLE", "currentness": "UNVERIFIED", "truth_claim": "CAPTURED_STABLE", "verification_mode": null, "verified_at": null, "verification_duration_ms": null, "verification_timings_ms": {}, "verification_attempts": 0, "mismatch_paths": [], "deltas": [], "diagnostics": [], "exclusions": [], "verification_scope": {}, "proof_contract_version": null, "verifier_version": null}, "availability": {"can_use_project": true, "can_use_snapshot": true, "can_search": false, "can_generate_exports": false, "can_generate_context": false}, "requires_user_action": false, "recommended_action": {"code": "NONE", "command": null, "available": true, "requires_user_approval": false, "reason": "snapshot_valid_when_published_currentness_unverified"}}, "warnings": [], "error": null, "meta": {"tool": "project-kb", "version": "0.1.0", "contract_version": 1}}

Interpretation: normal status exposes publication truth without claiming a post-publication comparison. It does not classify the snapshot as corrupt or rebuild-required.

## 6. FAST_VERIFICATION_REMOVED result

The real dogfood command `pkb status antique-attribution-ai --verify fast --json` was executed exactly once between the full before/after manifests. It did not interrupt the bounded runner and was followed by the strong samples.

Result contract:

- process exit: `2` (`USAGE_ERROR`);
- `ok=false`, `result=blocked`, code/error code `FAST_VERIFICATION_REMOVED`;
- snapshot status `verification_removed`;
- currentness `UNVERIFIED`, verification mode `fast`;
- no `CURRENT`, `STALE`, `verified_at`, duration, timing, or proof attempts (`0`);
- recommended action `USE_STRONG_VERIFICATION_OR_FULL_CAPTURE`, with `pkb status antique-attribution-ai --verify strong --json`;
- no target, live-Git, registry, snapshot, run/outcome, or lifecycle-pointer mutation.

Evidence distinction: the orchestration tool truncated the middle of its oversized first JSON capture after the process completed, so the byte-for-byte fast stdout/stderr strings were not retained in the final transcript. The result and exit mapping above are corroborated by the immediate fast short-circuit in `src/project_kb/resolver/project.py`, the stable policy in `src/project_kb/resolver/state.py`, `USAGE_ERROR = 2` in `src/project_kb/exit_codes.py`, the passing CLI contract test in `tests/test_stage6_currentness.py`, and the unchanged full manifest surrounding the one real invocation. The command was not repeated because the task explicitly required one invocation.

## 7. Five strong-verification samples

These are the five retained recovery samples. An earlier five-command evidence capture completed but its per-sample middle was truncated by the tool; no fast command was repeated. All retained commands were independent `pkb status antique-attribution-ai --verify strong --json` processes.

| Sample | Exit | Wall ms | Reported ms | Attempts | `verified_at` | Result |
| ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | 0 | 5055.8606 | 4501 | 1 | `2026-07-15T18:40:40.260915Z` | `CURRENT / CURRENT_AT_VERIFIED_TIME` |
| 2 | 0 | 5014.9406 | 4470 | 1 | `2026-07-15T18:40:45.308061Z` | `CURRENT / CURRENT_AT_VERIFIED_TIME` |
| 3 | 0 | 4949.6924 | 4415 | 1 | `2026-07-15T18:40:50.261845Z` | `CURRENT / CURRENT_AT_VERIFIED_TIME` |
| 4 | 0 | 5012.5152 | 4464 | 1 | `2026-07-15T18:40:55.272024Z` | `CURRENT / CURRENT_AT_VERIFIED_TIME` |
| 5 | 0 | 4983.6438 | 4477 | 1 | `2026-07-15T18:41:00.265721Z` | `CURRENT / CURRENT_AT_VERIFIED_TIME` |

Every sample returned `ok=true`, code `OK`, snapshot `b03ed877477c4445b0347e7695e98402`, `AVAILABLE`, `COMPATIBLE`, mode `strong`, proof contract `3`, verifier `3`, empty mismatch paths, empty deltas, empty warnings, and no error.

Common scope:

```json
{"included":["safe_text_content","candidate_population","bounded_metadata"],"excluded":["hard_secret_content","ignored_content","pruned_content"],"hard_secret_content_hashed":false}
```

Common explicit exclusion: `.venv` (content not verified). Common diagnostics honestly reported `STRONG_PROOF_EXCLUSIONS` and `GIT_STATUS_OUTSIDE_STRONG_SEMANTIC_STALE_PREDICATE`. The latter recorded an observer fingerprint change from `e3b0c442...b855` to `5fba8ee8...d7de`; it was outside the semantic stale predicate, while object/content proof, candidate population, target porcelain status, mismatch paths, and deltas remained stable.

## 8. Timing summary

- Median process wall duration: `5012.5152 ms`.
- Median reported verification duration: `4470 ms`.
- Range: wall `4949.6924–5055.8606 ms`; reported `4415–4501 ms`.

Interpretation: timing is evidence only. Correctness was stable across every retained sample.

## 9. Target repository before/after comparison

Before and after both showed HEAD `443a7a42478a09ad6a61a364b6ea817e44cb97ca`, branch `main`, and empty porcelain status. The repository root did not change. No target file, target code, target test, hook, service, or worktree operation was executed.

Result: pass, target unchanged and clean.

## 10. Live Git storage before/after comparison

| Evidence | Before | After |
| --- | --- | --- |
| Real index | 26,702 bytes, SHA-256 `f2b05520aa36945f2e2b0738afa8e7efd242c1e2ef156b8798092c3b8285022b` | identical |
| Repository config | 337 bytes, SHA-256 `5032255515c7f3c173a93fa2557a75814152a15f9e06c429b55a4f424b1ec7d9` | identical |
| Top-level Git files | 8 files, complete path/size/hash/mtime manifest | identical |
| `sharedindex.*` | absent | absent |
| `index.lock` | absent | absent |
| relevant locks/sidecars | absent | absent |

No Git object database was recursively hashed. Result: pass, no persistent live-Git mutation.

## 11. Registry/snapshot/managed-storage before/after comparison

| Evidence | Before | After |
| --- | --- | --- |
| Canonical snapshot | 7,057,408 bytes, SHA-256 `6b44fe20b847a5bbd4693547fbab99a935a54406910c95c4c40b03643d4545b6` | identical |
| Registry | 36,864 bytes, SHA-256 `37d298aa063b94ea11b1e1083fe2e33e457d107f22f42d82b2fa2a36867d2797` | identical |
| Managed storage | 17-file path/size/hash/mtime manifest | identical |
| Run/outcome files | 6-file path/size/hash/mtime manifest | identical |
| Registry/snapshot sidecars | no new family member | unchanged |
| `project-kb-git-index-*` directories | none | none |

Result: pass. Status and verification neither created nor moved a lifecycle pointer and left no managed temporary artifact.

## 12. Final pytest/static/CLI validation

| Command | Result | Duration / output |
| --- | --- | --- |
| `uv run pytest` | pass | 275 passed, 3 skipped, 1 warning; 414.73 s pytest / 415.9 s process |
| `uv run ruff format --check .` | pass | 58 files already formatted; 77.88 ms |
| `uv run ruff check .` | pass | all checks passed; 61.95 ms |
| `git diff --check` | pass | no whitespace error; 64.38 ms |
| `uv run pkb --help` | pass | exit 0; 310.01 ms |
| `uv run pkb status --help` | pass | exit 0; 295.11 ms; advertises strong proof and explicit legacy-fast removal |
| `uv run pkb index --help` | pass | exit 0; 302.25 ms |

## 13. Environment skips and residual risks

Facts:

- Three Windows tests skipped because this environment could not create the required file/directory symlinks: `test_indexing.py`, `test_registry_migration.py`, and `test_status_cli.py`.
- Pytest emitted one `PytestCacheWarning` because `.pytest_cache\v\cache\nodeids` could not be created over an existing path (`WinError 183`). Test execution and results were unaffected.
- `git diff --check` emitted only LF-to-CRLF conversion notices for existing modified files; it exited 0 with no whitespace error.
- Strong proof deliberately excludes `.venv`, ignored content, pruned content, and hard-secret content; `hard_secret_content_hashed=false`.

Unverified point / low evidence risk:

- The first orchestration response truncated the directly retained fast stdout/stderr and the first five strong timing bodies. The fast command was not repeated; its stable result is independently corroborated as described in section 6. Five new strong samples and fresh manifests were retained completely.

Assumptions: no external process changed either repository or managed storage during the bounded checks. Repeated Git status and byte manifests provide direct evidence for this assumption.

No blocking or high finding was discovered. The residuals above are low, declared, and do not weaken the observed strong-currentness result.

## 14. Stage 6 retained guarantees

The evidence supports retention of the current semantic-v2 snapshot, version-first compatibility, repository binding, canonical identity, proof manifest, bounded scanner/hard-secret policy, atomic publication history, machine-readable status errors, authoritative strong verification, safe Git subprocess behavior, and no-live-Git-storage-write guarantee. This report did not re-prove publication failure paths individually; the full 275-test pass supplies the repository regression gate.

Interpretation: all ten closure criteria in the task envelope are met. This report may recommend Stage 6 closure to Command Center, but it does not grant closure approval.

## 15. Stage 7 handoff boundary

Stage 7 Task Snapshot Lifecycle remains out of scope. No generation storage, task identity, baseline/working pointer, promotion, retention, comparison, incremental refresh, hook, watcher, or lifecycle implementation was inspected or authorized.

Recommended next safe step: Command Center Review of this closure report and the final cumulative implementation diff.

## 16. Final Git state

`project-kb` remained at `5af6736257f95bb958534b7cc74525200d81dc01` on `main`; staging remained empty. Its full non-ignored status remained the same expected Stage 6 surface:

```text
M README.md
M docs/DATA_SAFETY_POLICY.md
M docs/DEVELOPMENT_CHECKLIST.md
M docs/PROJECT_CONTEXT.md
M docs/PROJECT_MAP.md
M src/project_kb/cli/app.py
M src/project_kb/gating/models.py
M src/project_kb/gating/policy.py
M src/project_kb/git_utils.py
M src/project_kb/indexing/extractor.py
M src/project_kb/indexing/models.py
M src/project_kb/indexing/query.py
M src/project_kb/indexing/scanner.py
M src/project_kb/indexing/service.py
M src/project_kb/registry/models.py
M src/project_kb/registry/schema.py
M src/project_kb/registry/service.py
M src/project_kb/resolver/models.py
M src/project_kb/resolver/project.py
M src/project_kb/resolver/repo_identity.py
M src/project_kb/resolver/state.py
M src/project_kb/snapshot/database.py
M tests/conftest.py
M tests/test_gating.py
M tests/test_indexing.py
M tests/test_indexing_fixloop.py
M tests/test_registry_cli.py
M tests/test_registry_migration.py
M tests/test_status_cli.py
?? src/project_kb/indexing/identity.py
?? src/project_kb/indexing/module_map.py
?? src/project_kb/snapshot/currentness.py
?? src/project_kb/snapshot/schema_v2.py
?? tests/_git_support.py
?? tests/_stage6_support.py
?? tests/fixtures/stage6/v1_snapshot.sql
?? tests/test_stage6_currentness.py
?? tests/test_stage6_identity.py
?? tests/test_stage6_safety.py
?? tests/test_stage6_snapshot_trust.py
?? tests/test_validation_hygiene.py
```

That is 30 modified tracked paths, 12 untracked paths, and no staged path. The closure report itself is ignored analysis evidence. The target remained at `443a7a42478a09ad6a61a364b6ea817e44cb97ca` on `main`, clean.

## 17. Explicit non-approval boundary

No index, implementation change, test change, durable-documentation change, snapshot/registry mutation, target mutation, staging, commit, reset, checkout, stash, branch change, push, network access, or fetch occurred during this task. Only this one ignored closure report was created.

Recommendation: Stage 6 closure criteria are met and the report may advance to Command Center Review. This is not implementation review, commit approval, merge approval, release approval, deployment approval, Stage-closure approval, or authorization for Stage 7.
