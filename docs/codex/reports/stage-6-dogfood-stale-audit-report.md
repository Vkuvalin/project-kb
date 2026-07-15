# Stage 6 — Dogfood Immediate STALE Audit

Boundary: this report is preserved read-only audit evidence. It is not source of truth and does not authorize implementation, architecture changes, staging, commit, push, merge, release, or deployment.

## 1. Metadata and inspected boundary

Facts:

- Task envelope: `docs/codex/tasks/stage-6-dogfood-stale-audit-task.md`.
- Target project: `antique-attribution-ai`.
- Audit date: 2026-07-15.
- Route: `L3 Independent Checks`, resolved to `docs/codex/reasoning/L3_self_consistency.md`; no reroute was required.
- Procedures: `reasoning-patterns`, `audit-only`, `report-writing`, and `subagent-routing`.
- Independent read-only checks: `architecture_auditor` for the production ownership/call path and `test_auditor` for currentness regression coverage. Neither agent wrote files or approved implementation.
- Expected and observed `project-kb` base: HEAD `5af6736257f95bb958534b7cc74525200d81dc01`, branch `main`, tracking `origin/main`, empty staging, and the expected complete uncommitted Stage 6 tree: 28 tracked modifications plus 10 untracked implementation/test paths.
- Inspected boundary: all six preserved dogfood artifacts; the approved Stage Brief, ExecPlan, and cumulative implementation report; canonical `kb.sqlite`; registry SQLite; the successful run record; bounded live target Git/repository observations; the production indexing/status/validation/currentness/Git path; and focused currentness/safety tests.
- Canonical and registry SQLite were inspected through read-only connections. Production snapshot validation opened the canonical database with `mode=ro`.

Not inspected:

- No dogfood after-state file or ZIP exists. Their absence is consistent with the script aborting after fast verification 1.
- The preserved Git-storage oracle inventories top-level persistent files in the Git/common directory. It does not inventory every file below object, ref, or log subdirectories.
- Hard-secret, ignored, and pruned content was not opened or hashed. `.venv` remained an explicit proof exclusion.

## 2. Executive conclusion and classification

Primary classification:

```text
FALSE_POSITIVE_CURRENTNESS_DEFECT
```

Facts:

- Semantic-v2 publication succeeded and is internally valid, registry-aligned, repository-bound, and contract-compatible.
- The target HEAD, branch, candidate population, repository identity, binding generation, visibility flags, safe-text proofs, and bounded metadata proofs match the persisted snapshot.
- The live Git index reports a clean worktree. Repeated strong verification returns `CURRENT` with no semantic delta.
- Repeated fast verification returns the same `STALE` solely because it compares a publication-time status fingerprint produced against the live Git index with a verification-time status fingerprint produced against a reconstructed temporary index.
- The reconstructed-index payload contains 83 false ` M` records. Every one of the 83 worktree files differs from its index blob only by CRLF versus LF bytes; no other byte difference was found.

Interpretation:

The repository did not supply a concrete semantic delta. The verifier compared fingerprints from different observation protocols and mislabeled the protocol discrepancy as repository staleness. This violates the approved rules that `STALE` requires a concrete mismatch and that scanner and verifier share consistent Git state semantics. The defect fails closed, so it does not create false `CURRENT`, but it blocks trusted consumers and recommends an unnecessary reindex.

Evidence confidence: high. The exact preserved `after` hash was reproduced from the production temporary-index path, and independent strong proof twice returned `CURRENT`.

Primary finding severity: high (`P1` in the native auditor scale).

## 3. Preserved dogfood artifact inventory

All expected minimum artifacts existed before inspection and remained untouched.

| Artifact | Bytes | SHA-256 | Observation |
|---|---:|---|---|
| `stage-6-dogfood-antique-attribution-ai-source-before.json` | 106 | `3aeaaadf3847c812ffdb0455b78ba0311fa2f0bb03590ca72c2310a396ba8f85` | HEAD `443a7a...`, branch `main`, empty status |
| `stage-6-dogfood-antique-attribution-ai-git-storage-before.json` | 2632 | `657cf32e4beca08d4655d6283992a196d0b4b5128d665f3da4c7a6c2883e61b6` | Eight top-level persistent Git files, including index/config hashes; no `sharedindex.*` |
| `stage-6-dogfood-antique-attribution-ai-index.json` | 2388 | `5f0cf5622979b1c2e50a284df45c7fce27179e397fb065ae52d3dbb18adaffa9` | Successful atomic semantic-v2 publication with one warning |
| `stage-6-dogfood-antique-attribution-ai-index.json.stderr.log` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | Empty |
| `stage-6-dogfood-antique-attribution-ai-fast-1.json` | 3167 | `b8398ad654373894e84d4965a033e687819cc390b7c9b4c5e527863e29299b49` | Preserved immediate false `STALE` payload |
| `stage-6-dogfood-antique-attribution-ai-fast-1.json.stderr.log` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | Empty |

Missing artifacts:

- No expected minimum artifact is missing.
- No source-after, Git-storage-after, later fast/strong payload, or ZIP exists. This is expected for the recorded abort point, but it limits retrospective temporal claims as described in section 12.

## 4. Exact fast-1 STALE payload

The following values are preserved in `stage-6-dogfood-antique-attribution-ai-fast-1.json` and are not normalized or generalized:

```json
{
  "status": "currentness_verification_completed",
  "snapshot_id": "b03ed877477c4445b0347e7695e98402",
  "indexed_at": "2026-07-15T01:02:57.773341Z",
  "git_commit_at_index": "443a7a42478a09ad6a61a364b6ea817e44cb97ca",
  "current_git_commit": "443a7a42478a09ad6a61a364b6ea817e44cb97ca",
  "reason": "git_status_fingerprint_changed",
  "availability": "AVAILABLE",
  "compatibility": "COMPATIBLE",
  "currentness": "STALE",
  "verification_mode": "fast",
  "verified_at": "2026-07-15T01:03:01.444069Z",
  "verification_duration_ms": 1944,
  "verification_timings_ms": {"fast": 1944},
  "verification_attempts": 1,
  "mismatch_paths": [],
  "deltas": [
    {
      "kind": "GIT_STATUS_CHANGED",
      "before": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "after": "5fba8ee83c25328da7a9bc9ce677568397aa9e3d2343ae6a6c7b26a58901d7de"
    }
  ],
  "diagnostics": [],
  "exclusions": [".venv"]
}
```

Additional exact payload context:

- Repository check: `valid`; stored/current Git root match; strong fingerprint `matched`; reason `strong_fingerprint_matched`.
- Repository binding/identity status: matched. The verifier would have returned earlier for a binding or identity mismatch.
- HEAD comparison: equal.
- Branch: persisted and live `main`; no branch diagnostic.
- Candidate/population comparison: no delta was emitted. Direct audit observation reproduced the persisted candidate fingerprint `ee2dce00159011cf997b6b877544b5e52b58868fb1ebac7a7eb241a57db0d686` with 237 candidates: 236 `TRACKED` plus one `POLICY_DISCOVERED` pruned root.
- Visibility/index seal: persisted and live visibility paths are empty; the reproduced observation was sealed.
- Proof contract: compatible; details are in section 5.
- The reproduced raw temporary-index status payload is 3,787 bytes and contains exactly 83 records, all ` M`: 2 repository-root files, 2 scripts, 49 `src/` files, and 30 `tests/` files. Its SHA-256 is the exact preserved `after` value `5fba8ee8...1d7de`.

Interpretation:

`mismatch_paths: []` is not evidence of a hidden changed file. The `GIT_STATUS_CHANGED` object contains no `path`, and `_finish()` derives mismatch paths only from deltas that contain one (`src/project_kb/snapshot/currentness.py:582-611`).

## 5. v2 publication and registry state

Facts:

- Index result: `INDEX_PUBLISHED_WITH_WARNINGS`; run `3bec1274f1e543fe9a27ddc6789c33ca`; snapshot `b03ed877477c4445b0347e7695e98402`.
- Canonical publication: `published: true`, `atomic: true`, state `POST_PUBLICATION_RECORDED`.
- Previous snapshot: `preserved: false`, `replaced: true`. The prior canonical v1 was atomically replaced by v2.
- Schema/contract versions: schema `2`; scanner `stage6-v2`; policy `stage4-v0-3`; extractor `python-ast:1`; proof `3`; verifier `3`; module map `4`; occurrence `2`.
- Snapshot state: `SEALED`; run row `BUILD_SUCCEEDED`; canonical size 7,057,408 bytes; audit SHA-256 `6b44fe20b847a5bbd4693547fbab99a935a54406910c95c4c40b03643d4545b6`.
- Independent read-only `validate_snapshot(..., require_v2=True)` result: `valid_v2`; `readable`, schema, scanner, policy, extractor, proof, verifier, module-map, and occurrence compatibility are all `true`.
- Registry live state: `last_status=INDEX_SUCCEEDED`; `last_indexed_at=2026-07-15T01:02:57.773341Z`; last Git commit `443a7a...`; active and snapshot binding generations both `<PROJECT_ID>`.
- Registry event `72fce4fc1c23474fafc02b93fccaa35c` records successful index outcome at `2026-07-15T01:02:58Z`.
- Run file `runs/3bec1274f1e543fe9a27ddc6789c33ca.json` records `SUCCESS`, `publication_state=PUBLISHED`, and the same snapshot ID and counts.
- The preserved index response embeds the project record resolved before the run, so its nested `project.last_status` still shows the preceding failed attempt. Its publication bookkeeping states `registry: recorded`, and both the immediate fast payload and live registry show the reconciled successful state. This is not a publication/registry disagreement.

Interpretation:

Publication and registry state agree. There is no evidence for `PUBLICATION_OR_REGISTRY_CONTRACT_DEFECT`.

## 6. Persisted proof versus live repository comparison

| Proof dimension | Persisted v2 | Live/audit evidence | Result |
|---|---|---|---|
| Repository root | `<TARGET_REPO>` | Same resolved root | Match |
| Repository identity hash | `478f1853cfce216b1ab9611131780d03d74fea9cdb8018d748a9f3c76ba626e6` | Strong live identity matched | Match |
| Binding generation | `<PROJECT_ID>` | Registry active/snapshot generation same | Match |
| HEAD | `443a7a42478a09ad6a61a364b6ea817e44cb97ca` | Same | Match |
| Branch | `main` | `main` | Match; diagnostic only in any case at same HEAD |
| Candidate fingerprint | `ee2dce00159011cf997b6b877544b5e52b58868fb1ebac7a7eb241a57db0d686` | Same | Match |
| Candidate population | 237 | 236 tracked + 1 policy-discovered pruned root | Match |
| Live-index worktree status | Empty hash `e3b0...b855` | Live index reports no status paths | Match |
| Neutral temporary-index status | Not used at publication | Hash `5fba...1d7de`, 83 false ` M` records | Non-comparable observer result |
| Visibility flags | Empty | Empty, sealed observations | Match |
| Safe-text proof | 234 `CONTENT_HASH/TEXT` rows | Strong verification twice: no object delta | Match |
| Bounded metadata | 2 `BOUNDED_METADATA/UNSUPPORTED` rows | Strong verification twice: no metadata delta | Match |
| Exclusions | One `.venv` pruned-root proof | `.venv`; content not verified | Match declared scope |
| Contract versions | schema 2; proof 3; verifier 3; module 4; occurrence 2 | All current constants equal | Compatible |

Facts:

- Proof manifest totals: 237 rows: 234 safe-text content hashes, 2 bounded-metadata proofs, and 1 excluded pruned root.
- Strong verification returned `CURRENT` twice and explicitly classified the status hash difference as `GIT_STATUS_OUTSIDE_STRONG_SEMANTIC_STALE_PREDICATE`.

Interpretation:

The first exact mismatch encountered by fast verification is not a repository proof mismatch. It is the comparison of a live-index-derived persisted status hash with a temporary-index-derived verification status hash.

## 7. Source-repository/live-Git-storage assessment

Endpoint facts:

- Preserved pre-dogfood source state: HEAD `443a7a...`, branch `main`, empty status.
- Audit-time source state after all reproductions: the same HEAD and branch, no live-index status paths, and no visibility flags.
- The audit-time top-level Git/common-directory file inventory exactly matches `git-storage-before.json`: the same eight paths, lengths, and SHA-256 values.
- Real index before and audit-time: 26,702 bytes, SHA-256 `F2B05520AA36945F2E2B0738AFA8E7EFD242C1E2EF156B8798092C3B8285022B`.
- Repository-local config before and audit-time: 337 bytes, SHA-256 `5032255515C7F3C173A93FA2557A75814152A15F9E06C429B55A4F424B1EC7D9`.
- `HEAD`, `COMMIT_EDITMSG`, `FETCH_HEAD`, `ORIG_HEAD`, `description`, and `packed-refs` also match byte-for-byte at the two observed endpoints.
- No `sharedindex.*` existed before or at audit time.
- Repeated fast/strong verification and direct temporary-index inspection left the same target index/config/common-directory endpoint hashes.

What is proven:

- No persistent endpoint change is visible in the target source state or in the preserved Git-storage comparison boundary.
- The current source matches the persisted strong proof and produces no semantic delta.

What is not proven retrospectively:

- Because no immediate source-after or Git-storage-after artifact was persisted, the evidence cannot prove that no transient mutation occurred and was restored between the before capture and the audit-time capture.
- The preserved Git-storage oracle does not cover every nested object/ref/log file.

Interpretation:

There is no evidence that indexing changed target source or live Git storage. The exact false status hash is reproducible from the isolated verifier view while the live index and storage endpoints remain unchanged. The dogfood wrapper did not create the defect.

## 8. Production call path and code evidence

1. CLI: `src/project_kb/cli/app.py:63-107` — `status()` passes `--verify fast` to `ProjectStatusService.status()` and serializes the authoritative outcome.
2. Resolver/status: `src/project_kb/resolver/project.py:59-68`, `:209-256` — resolves the project, validates repository/storage, and calls `validate_snapshot()` with project/root/identity/binding expectations.
3. Snapshot validation: `src/project_kb/snapshot/database.py:733-852` — opens SQLite read-only, performs integrity/version/schema/binding/compatibility/invariant checks, and returns valid v2 metadata.
4. Currentness dispatch: `src/project_kb/resolver/project.py:337-395` — calls `verify_snapshot_currentness()` and maps its result into `SnapshotCheck`; `:434-442` maps `STALE` to `SNAPSHOT_STALE`.
5. Persisted publication observation: `src/project_kb/indexing/service.py:114-126`, `:174-180` calls `git_candidates()` and `capture_repo_state()` against the live index for before/after/final checks. `src/project_kb/indexing/scanner.py:117-139` hashes live-index porcelain status. `src/project_kb/snapshot/database.py:347-421` persists that hash and the before/after states.
6. Verification observation: `src/project_kb/snapshot/currentness.py:116-212`, `:225-280`, `:614-626` loads the proof/stored state and performs three sealed fast observations through `capture_repo_observation()`.
7. Neutral-index construction: `src/project_kb/indexing/scanner.py:142-212` hashes status through an isolated `GIT_INDEX_FILE`; `src/project_kb/git_utils.py:188-249` reconstructs that index from `ls-files --stage -z`, omitting live stat-cache/layout/visibility extensions.
8. Delta classification: `src/project_kb/snapshot/currentness.py:795-839` finds no HEAD/candidate delta. `:382-402` then compares the non-equivalent status hashes and returns `STALE`, `git_status_fingerprint_changed`, and `GIT_STATUS_CHANGED`.
9. Output/gating: `src/project_kb/resolver/project.py:374-469` copies the result to public status, blocks current-only consumers, and returns the reindex action serialized by the CLI.

Contract evidence:

- Stage Brief `docs/codex/stage_briefs/stage-6-trust-substrate-brief.md:127-135`: `STALE` requires a concrete mismatch; insufficient proof is `UNVERIFIED`.
- Stage Brief `:345-348`: scanner and verifier must share one Git observation boundary and consistent state semantics.
- Stage Brief `:519-534`: fast verification's safe case includes same binding, compatible proof, same HEAD, clean index/verify states, same candidate population, and stable observations.

Interpretation:

The code follows its literal branch logic but violates the approved cross-layer currentness contract. This is a currentness proof defect, not a registry or publication transition defect.

## 9. Safe reproduction results

Allowed read-only checks were run only after the preserved artifacts and databases were inspected.

| Check | Result |
|---|---|
| `pkb status antique-attribution-ai --json` | Valid published v2; `AVAILABLE`, `COMPATIBLE`, currentness `UNVERIFIED` because no proof requested |
| Fast verification 1 | `STALE`; 1 attempt; 1436 ms; exact reason/delta/hashes from preserved fast-1 |
| Fast verification 2 | `STALE`; 1 attempt; 1726 ms; identical reason/delta/hashes |
| Strong verification 1 | `CURRENT`; 1 attempt; 3404 ms; no deltas; status-only diagnostic; `.venv` exclusion |
| Strong verification 2 | `CURRENT`; 1 attempt; 3885 ms; same result and diagnostics |
| Direct neutral-index status capture | 3,787 bytes; SHA-256 `5fba...1d7de`; 83 ` M` records |
| Direct live-index status paths | Empty |
| Candidate/observation capture | 237 candidates; persisted fingerprint match; no flags; sealed |
| Independent v2 validation | `valid_v2`; every compatibility field true |

Byte-level reproduction:

- For each of the 83 neutral-index ` M` paths, the worktree bytes were compared with the stage-0 index blob.
- Result: 0 exact byte-equal, 83 CRLF-only differences, 0 other differences.
- Replacing CRLF with LF makes every worktree file byte-equal to its index blob.
- Representative `.gitignore`: index blob 5,129 bytes with 261 LF and no CRLF; worktree 5,390 bytes with 261 CRLF; LF-normalized bytes are equal.

Stability conclusion:

- Fast and strong are individually stable but disagree because they apply different semantic predicates to the same neutral-index status artifact.
- Strong proof is the authoritative evidence that persisted safe objects and candidate population remain current within the declared scope.

## 10. Root cause

Facts:

1. The target has 83 tracked worktree text files whose normal worktree bytes are CRLF while their index blobs are LF.
2. Publication calls `capture_repo_state()` against the live Git index. Its cached live-index observation produced the clean hash `e3b0...b855`, persisted in v2.
3. Fast verification reconstructs a temporary index from stage entries. The reconstruction deliberately omits live index stat-cache/layout extensions and is evaluated through the sanitized Git environment, which sets `GIT_CONFIG_GLOBAL` to the null device.
4. This temporary view forces Git to compare the CRLF worktree bytes with LF index blobs without the checkout conversion context that produced the clean live-index state. Git emits 83 ` M` records whose raw payload hashes to `5fba...1d7de`.
5. The verifier treats inequality between the live-index publication hash and temporary-index verification hash as a concrete `STALE`, even though HEAD, candidates, identity, binding, visibility, and strong object proof all match.

Exact root cause:

```text
Non-comparable live-index and reconstructed-index Git-status fingerprints are
compared as if they shared one observation protocol. Windows CRLF/LF checkout
normalization makes the reconstructed neutral index report 83 false worktree
modifications, and fast verification misclassifies that observer artifact as STALE.
```

Risk:

- False `STALE` blocks search/export/context gates and recommends a needless full reindex.
- Reindexing with the same observation mismatch does not solve the defect; the first fast check can fail identically again.
- The defect is fail-closed and therefore does not create false `CURRENT`.

## 11. Required or unnecessary fix boundary

Implementation change required: yes.

Smallest safe correctness boundary:

- Do not classify a hash-only status difference as `STALE` when the persisted and live hashes come from different observation protocols and there is no HEAD, candidate, path, or object-proof delta.
- Until one comparable status protocol exists, return `UNVERIFIED` with an explicit observer/proof diagnostic and recommend strong verification rather than reindex.

Durable correction boundary, subject to a separately approved implementation task:

- Make publication and verification consume one versioned/provenance-aware status observation semantics, or refuse cross-protocol fingerprint comparison.
- Keep ownership within `src/project_kb/indexing/service.py`, `indexing/scanner.py`, `git_utils.py`, and `snapshot/currentness.py`; touch snapshot contract/version handling only if the proof representation changes.
- Preserve path/status evidence when a concrete fast status delta is available instead of emitting only a pathless hash delta.
- Add a real-Git Windows regression whose index is created with checkout EOL conversion and whose verification runs through the production sanitized Git boundary. Assert immediate fast does not return false `STALE`, strong remains `CURRENT`, and live Git storage is unchanged.
- Strengthen the existing fast stale test to assert exact reason, delta, hashes, and path behavior. Current disposable fixtures add/commit under the same no-global-config environment and do not cover this converted-worktree boundary.

Dogfood script correction:

- Not required for the root cause. The script correctly surfaced the production defect and preserved the minimum expected evidence.
- Optional evidence improvement: persist immediate source-after/Git-storage-after plus bounded raw status records before aborting. This would improve attribution but is not the product fix.

Snapshot/reindex recommendation:

- Keep the current v2 snapshot available. It is valid, registry-aligned, and twice strongly verified `CURRENT` within its declared exclusions.
- Do not perform another full index before the fix; it is unnecessary and likely to reproduce the same false fast result.
- After an approved fix, rerun fast and strong against the existing snapshot first. If the fix advances the proof/verifier contract, the normal compatibility policy will require one full reindex; otherwise no reindex is inherently required.

## 12. Residual uncertainty

Unverified points:

- No immediate after-state files exist, so the audit cannot prove the absence of a transient target or Git-storage mutation that was later restored.
- The before/current Git-storage comparison is limited to the preserved oracle's top-level persistent-file boundary.
- The source of the target's checkout EOL policy was not read from user-global configuration. It is unnecessary for diagnosis: the CRLF worktree/LF blob relation and the observer-specific payload are directly proven.
- `.venv` remains excluded and its content was not verified.

Why these points do not change the classification:

- The exact preserved false-status hash is reproducible at audit time solely through the production neutral-index observer while the live index is clean.
- All 83 reported records are CRLF-only observer differences.
- Candidate, binding, identity, HEAD, visibility, and safe object proof match; strong verification twice returns `CURRENT`.

Assumptions:

- The preserved artifact timestamps and contents correspond to the dogfood sequence named in the task envelope.
- The approved strong verifier's declared proof scope is the intended semantic currentness authority for safe text and bounded metadata; hard-secret/ignored/pruned exclusions remain outside that claim.

## 13. Checks run

Read-only/evidence checks:

- Loaded `AGENTS.md`, the task envelope, resolved L3 route, Stage Brief, ExecPlan, cumulative report, and required skills in bootstrap order.
- Recorded `git status --porcelain=v2 --branch --untracked-files=all` and empty `git diff --cached --name-status` for `project-kb`.
- Inventoried, read, and SHA-256 hashed all six dogfood artifacts.
- Inspected canonical SQLite tables, schema, snapshot metadata, run row, proof summaries, and counts through immutable/read-only SQLite URIs.
- Inspected registry schema, project row, binding generations, and event history through an immutable/read-only SQLite URI.
- Read the matching run JSON.
- Ran production `validate_snapshot(..., require_v2=True)` read-only.
- Ran allowed status, two fast, and two strong verifications.
- Captured live status paths, visibility paths, HEAD, branch, candidate/population fingerprints, and the sealed neutral-index status through bounded Git tooling.
- Compared all 83 neutral-status worktree files to their stage-0 index blobs without writing either side.
- Compared the preserved before and audit-time top-level Git-storage path/length/SHA-256 inventories.
- Traced the exact CLI, resolver, snapshot validation, currentness, repository observation, and delta-classification symbols.
- Performed independent architecture-path and test-coverage audits and consolidated only evidence-backed findings.

Not run:

- No index command.
- No broad or focused pytest suite; runtime dogfood reproduction directly established the defect, and test inspection established the missing EOL/observer-parity regression.
- No network, fetch, target execution, or secret-content check.

## 14. Git status and explicit non-modification confirmation

Audit-start `project-kb` state:

- HEAD `5af6736257f95bb958534b7cc74525200d81dc01`.
- Branch `main`, tracking `origin/main`, ahead/behind `+0/-0`.
- Staging empty.
- Expected Stage 6 worktree only: 28 tracked modifications and 10 untracked implementation/test paths; no unrelated tracked drift.

Audit non-modification evidence:

- All six protected dogfood artifact sizes and hashes remained unchanged.
- Canonical snapshot and registry sizes/hashes remained unchanged across audit verification.
- Target live index, repository-local config, and the preserved top-level Git-storage endpoint inventory remained byte/path identical to the before artifact.
- Target HEAD, branch, and clean live-index status remained unchanged.
- No managed temporary Git index remained after any observation.
- The only file created by this task is this explicitly authorized ignored audit report.

No index, implementation change, test change, config change, durable-doc change, ExecPlan change, cumulative-report change, dogfood-artifact change, registry/snapshot mutation, target mutation, staging, commit, reset, checkout, stash, branch change, push, network, or fetch was performed.

## 15. Non-approval boundary

This report diagnoses and classifies the dogfood failure only. It does not approve architecture, implementation, a fix design, code review, commit, merge, release, deployment, or another index run. Any correction requires a separate explicitly approved task with fixed scope and validation gates.
