"""Machine-verifiable currentness checks for semantic-v2 snapshots."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from project_kb.errors import ProjectKbError
from project_kb.indexing.models import (
    Candidate,
    ObjectEvidence,
    RepoObservation,
    RepoState,
    ScanPolicy,
)
from project_kb.indexing.policy import path_key
from project_kb.indexing.scanner import (
    RepositoryChangedError,
    ScanError,
    UnsafePathError,
    capture_repo_observation,
    compare_persisted_evidence,
    git_status_paths,
)


class CurrentnessState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"
    CHANGED_DURING_CHECK = "CHANGED_DURING_CHECK"
    ERROR = "ERROR"


class VerificationMode(StrEnum):
    """Public verification tokens; ``fast`` is retained only as a removed token."""

    FAST = "fast"
    STRONG = "strong"


@dataclass(frozen=True)
class RepositoryBindingObservation:
    repo_root_norm: str
    repository_identity_hash: str
    repository_binding_generation: str
    live_identity_token: str
    live_identity_matches: bool
    live_identity_reason: str

    @property
    def registry_binding(self) -> tuple[str, str, str]:
        return (
            self.repo_root_norm,
            self.repository_identity_hash,
            self.repository_binding_generation,
        )


@dataclass(frozen=True)
class CurrentnessResult:
    state: CurrentnessState
    mode: VerificationMode
    reason: str
    current_git_commit: str | None
    verified_at: str | None
    mismatch_paths: tuple[str, ...] = ()
    deltas: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    exclusions: tuple[str, ...] = ()
    attempts: int = 0
    duration_ms: int | None = None
    binding_status: str = "MATCHED"

    @property
    def timings_ms(self) -> dict[str, int]:
        if self.duration_ms is None:
            return {}
        return {self.mode.value: self.duration_ms}


@dataclass(frozen=True)
class _ProofEntry:
    relative_path: str
    path_key: str
    git_population: str
    proof_class: str
    evidence_kind: str
    stat_signature: tuple[int, int, int, int, int] | None
    content_hash: str | None

    def evidence(self) -> ObjectEvidence:
        return ObjectEvidence(
            relative_path=self.relative_path,
            evidence_kind=self.evidence_kind,
            stat_signature=self.stat_signature,
            content_hash=self.content_hash,
        )


_PROOF_CLASSES = {
    "CONTENT_HASH",
    "BOUNDED_METADATA",
    "EXCLUDED_HARD_SECRET",
    "EXCLUDED_PRUNED",
}


def verify_snapshot_currentness(
    snapshot_path: Path,
    *,
    repo_root: Path,
    snapshot_meta: dict[str, Any],
    mode: VerificationMode | str,
    policy: ScanPolicy | None = None,
    active_binding: Callable[[], RepositoryBindingObservation] | None = None,
    expected_binding: tuple[str, str, str] | None = None,
    max_attempts: int = 2,
) -> CurrentnessResult:
    """Verify one compatible v2 snapshot without importing or executing target code."""

    verification_mode = VerificationMode(mode)
    if verification_mode is VerificationMode.FAST:
        return CurrentnessResult(
            state=CurrentnessState.UNVERIFIED,
            mode=verification_mode,
            reason="fast_verification_removed",
            current_git_commit=None,
            verified_at=None,
            attempts=0,
            duration_ms=None,
            binding_status="NOT_CHECKED",
        )

    started_ns = time.perf_counter_ns()
    current_state: RepoState | None = None
    exclusions: tuple[str, ...] = ()
    try:
        policy = policy or _policy_from_meta(snapshot_meta)
        expected_binding = expected_binding or (
            snapshot_meta["repo_root_norm"],
            snapshot_meta["repository_identity_hash"],
            snapshot_meta["repository_binding_generation"],
        )
        proof_entries = _load_proof_manifest(
            snapshot_path,
            snapshot_id=snapshot_meta["snapshot_id"],
        )
        stored_state = _stored_repo_state(snapshot_meta)
        exclusions = tuple(
            sorted(
                entry.relative_path
                for entry in proof_entries
                if entry.proof_class in {"EXCLUDED_HARD_SECRET", "EXCLUDED_PRUNED"}
            )
        )
        expected_candidates = {
            entry.path_key: (entry.relative_path, entry.git_population) for entry in proof_entries
        }
        excluded_pruned_roots = tuple(
            entry.relative_path for entry in proof_entries if entry.proof_class == "EXCLUDED_PRUNED"
        )
        temporary_index_root = snapshot_path.parent
        if len(expected_candidates) != len(proof_entries):
            raise ValueError("proof manifest contains duplicate path identities")

        for attempt in range(1, max_attempts + 1):
            binding_before = _active_binding(active_binding, expected_binding)
            binding_status = _binding_status(binding_before, expected_binding)
            if binding_status != "MATCHED":
                return _finish(
                    started_ns,
                    verification_mode,
                    CurrentnessState.UNVERIFIED,
                    "repository_binding_mismatch"
                    if binding_status == "BINDING_MISMATCH"
                    else "repository_identity_mismatch",
                    current_state,
                    attempts=attempt,
                    diagnostics=(
                        {
                            "kind": binding_status,
                            "reason": binding_before.live_identity_reason,
                        },
                    ),
                    exclusions=exclusions,
                    binding_status=binding_status,
                )
            observation_before = _capture_observation(
                repo_root,
                policy,
                temporary_index_root,
            )
            if observation_before is None:
                if attempt < max_attempts:
                    continue
                return _unstable_index_result(
                    started_ns,
                    verification_mode,
                    current_state,
                    attempt,
                    exclusions,
                )
            candidates_before = observation_before.candidates
            state_before = observation_before.state
            current_state = state_before
            candidate_map_before = _candidate_map(candidates_before)
            relevant_candidates_before = _without_pruned_descendants(
                candidate_map_before,
                excluded_pruned_roots,
            )
            deltas = _repository_deltas(
                stored_state,
                state_before,
                expected_candidates,
                relevant_candidates_before,
            )
            observation_changed = False

            if verification_mode is VerificationMode.STRONG and not deltas:
                try:
                    deltas = _strong_object_deltas(
                        repo_root,
                        proof_entries,
                        policy,
                    )
                except RepositoryChangedError, UnsafePathError:
                    observation_changed = True

            observation_after = _capture_observation(
                repo_root,
                policy,
                temporary_index_root,
            )
            if observation_after is None:
                if attempt < max_attempts:
                    continue
                return _unstable_index_result(
                    started_ns,
                    verification_mode,
                    current_state,
                    attempt,
                    exclusions,
                )
            state_after = observation_after.state
            current_state = state_after
            binding_after = _active_binding(active_binding, expected_binding)
            stable = (
                not observation_changed
                and _repo_observations_stable(observation_before, observation_after)
                and _binding_observations_stable(
                    binding_before,
                    binding_after,
                    expected_binding,
                )
            )
            if stable and verification_mode is VerificationMode.STRONG and not deltas:
                seal_changed = False
                try:
                    seal_deltas = _strong_object_deltas(
                        repo_root,
                        proof_entries,
                        policy,
                    )
                except RepositoryChangedError, UnsafePathError:
                    seal_changed = True
                    seal_deltas = []
                observation_sealed = _capture_observation(
                    repo_root,
                    policy,
                    temporary_index_root,
                )
                if observation_sealed is None:
                    if attempt < max_attempts:
                        continue
                    return _unstable_index_result(
                        started_ns,
                        verification_mode,
                        current_state,
                        attempt,
                        exclusions,
                    )
                state_sealed = observation_sealed.state
                current_state = state_sealed
                binding_sealed = _active_binding(active_binding, expected_binding)
                stable = (
                    not seal_changed
                    and not seal_deltas
                    and _repo_observations_stable(observation_after, observation_sealed)
                    and _binding_observations_stable(
                        binding_after,
                        binding_sealed,
                        expected_binding,
                    )
                )
                observation_after = observation_sealed
                state_after = state_sealed
                binding_after = binding_sealed
            if not stable:
                if attempt < max_attempts:
                    continue
                return _finish(
                    started_ns,
                    verification_mode,
                    CurrentnessState.CHANGED_DURING_CHECK,
                    "repository_or_binding_changed_during_verification",
                    current_state,
                    attempts=attempt,
                    diagnostics=(
                        {
                            "kind": "UNSTABLE_OBSERVATION",
                            "binding_stable": _binding_observations_stable(
                                binding_before,
                                binding_after,
                                expected_binding,
                            ),
                        },
                    ),
                    exclusions=exclusions,
                )

            if state_after.index_visibility_paths:
                return _finish(
                    started_ns,
                    verification_mode,
                    CurrentnessState.UNVERIFIED,
                    "verification_rejects_index_visibility_flags",
                    current_state,
                    attempts=attempt,
                    diagnostics=(
                        {
                            "kind": "INDEX_VISIBILITY_FLAGS",
                            "paths": list(state_after.index_visibility_paths),
                        },
                    ),
                    exclusions=exclusions,
                )

            diagnostics = _proof_diagnostics(
                stored_state,
                state_after,
                mode=verification_mode,
                exclusions=exclusions,
            )
            if deltas:
                return _finish(
                    started_ns,
                    verification_mode,
                    CurrentnessState.STALE,
                    "concrete_repository_delta",
                    current_state,
                    attempts=attempt,
                    deltas=tuple(deltas),
                    diagnostics=diagnostics,
                    exclusions=exclusions,
                )

            if verification_mode is VerificationMode.STRONG:
                # The status-path probe is an explicit terminal boundary, never a
                # predicate for whether the full strong observation is required.
                # A clean commit can change HEAD while leaving porcelain empty.
                git_status_paths(repo_root)
                terminal_observation_before = _capture_observation(
                    repo_root,
                    policy,
                    temporary_index_root,
                )
                if terminal_observation_before is None:
                    if attempt < max_attempts:
                        continue
                    return _unstable_index_result(
                        started_ns,
                        verification_mode,
                        current_state,
                        attempt,
                        exclusions,
                    )
                terminal_changed = False
                terminal_object_deltas: list[dict[str, Any]] = []
                try:
                    terminal_object_deltas = _strong_object_deltas(
                        repo_root,
                        proof_entries,
                        policy,
                    )
                except RepositoryChangedError, UnsafePathError:
                    terminal_changed = True
                terminal_observation_after = _capture_observation(
                    repo_root,
                    policy,
                    temporary_index_root,
                )
                if terminal_observation_after is None:
                    if attempt < max_attempts:
                        continue
                    return _unstable_index_result(
                        started_ns,
                        verification_mode,
                        current_state,
                        attempt,
                        exclusions,
                    )
                terminal_candidates_after = terminal_observation_after.candidates
                terminal_state_after = terminal_observation_after.state
                current_state = terminal_state_after
                terminal_map_after = _candidate_map(terminal_candidates_after)
                binding_terminal = _active_binding(active_binding, expected_binding)
                terminal_stable = (
                    not terminal_changed
                    and _repo_observations_stable(
                        terminal_observation_before,
                        terminal_observation_after,
                    )
                    and _binding_observations_stable(
                        binding_after,
                        binding_terminal,
                        expected_binding,
                    )
                )
                if not terminal_stable:
                    if attempt < max_attempts:
                        continue
                    return _finish(
                        started_ns,
                        verification_mode,
                        CurrentnessState.CHANGED_DURING_CHECK,
                        "repository_or_binding_changed_during_terminal_proof",
                        current_state,
                        attempts=attempt,
                        diagnostics=(
                            {
                                "kind": "UNSTABLE_TERMINAL_PROOF",
                                "binding_stable": _binding_status(
                                    binding_terminal,
                                    expected_binding,
                                )
                                == "MATCHED",
                            },
                        ),
                        exclusions=exclusions,
                    )
                if terminal_state_after.index_visibility_paths:
                    return _finish(
                        started_ns,
                        verification_mode,
                        CurrentnessState.UNVERIFIED,
                        "verification_rejects_index_visibility_flags",
                        current_state,
                        attempts=attempt,
                        diagnostics=(
                            {
                                "kind": "INDEX_VISIBILITY_FLAGS",
                                "paths": list(terminal_state_after.index_visibility_paths),
                            },
                        ),
                        exclusions=exclusions,
                    )
                terminal_relevant_candidates = _without_pruned_descendants(
                    terminal_map_after,
                    excluded_pruned_roots,
                )
                terminal_deltas = _repository_deltas(
                    stored_state,
                    terminal_state_after,
                    expected_candidates,
                    terminal_relevant_candidates,
                )
                terminal_deltas.extend(terminal_object_deltas)
                diagnostics = _proof_diagnostics(
                    stored_state,
                    terminal_state_after,
                    mode=verification_mode,
                    exclusions=exclusions,
                )
                if terminal_deltas:
                    return _finish(
                        started_ns,
                        verification_mode,
                        CurrentnessState.STALE,
                        "concrete_repository_delta_at_terminal_proof",
                        current_state,
                        attempts=attempt,
                        deltas=tuple(terminal_deltas),
                        diagnostics=diagnostics,
                        exclusions=exclusions,
                    )

            return _finish(
                started_ns,
                verification_mode,
                CurrentnessState.CURRENT,
                "strong_proof_satisfied",
                current_state,
                attempts=attempt,
                diagnostics=diagnostics,
                exclusions=exclusions,
            )
    except (
        ScanError,
        ProjectKbError,
        OSError,
        sqlite3.Error,
        subprocess.CalledProcessError,
        ValueError,
        KeyError,
    ) as exc:
        return _finish(
            started_ns,
            verification_mode,
            CurrentnessState.ERROR,
            "verification_error",
            current_state,
            attempts=0,
            diagnostics=({"kind": "ERROR", "error_type": type(exc).__name__},),
            exclusions=exclusions,
        )


def _finish(
    started_ns: int,
    mode: VerificationMode,
    state: CurrentnessState,
    reason: str,
    repo_state: RepoState | None,
    *,
    attempts: int,
    deltas: tuple[dict[str, Any], ...] = (),
    diagnostics: tuple[dict[str, Any], ...] = (),
    exclusions: tuple[str, ...] = (),
    binding_status: str = "MATCHED",
) -> CurrentnessResult:
    mismatch_paths = tuple(
        sorted({str(delta["path"]) for delta in deltas if delta.get("path") is not None})
    )
    return CurrentnessResult(
        state=state,
        mode=mode,
        reason=reason,
        current_git_commit=repo_state.head if repo_state else None,
        verified_at=_utc_now(),
        mismatch_paths=mismatch_paths,
        deltas=deltas,
        diagnostics=diagnostics,
        exclusions=exclusions,
        attempts=attempts,
        duration_ms=(time.perf_counter_ns() - started_ns) // 1_000_000,
        binding_status=binding_status,
    )


def _capture_observation(
    repo_root: Path,
    policy: ScanPolicy,
    temporary_root: Path,
) -> RepoObservation | None:
    try:
        return capture_repo_observation(
            repo_root,
            policy,
            temporary_root=temporary_root,
        )
    except RepositoryChangedError:
        return None


def _unstable_index_result(
    started_ns: int,
    mode: VerificationMode,
    repo_state: RepoState | None,
    attempt: int,
    exclusions: tuple[str, ...],
) -> CurrentnessResult:
    return _finish(
        started_ns,
        mode,
        CurrentnessState.CHANGED_DURING_CHECK,
        "git_index_generation_changed_during_verification",
        repo_state,
        attempts=attempt,
        diagnostics=({"kind": "UNSTABLE_GIT_INDEX_GENERATION"},),
        exclusions=exclusions,
    )


def _repo_observations_stable(before: RepoObservation, after: RepoObservation) -> bool:
    return (
        before.sealed
        and after.sealed
        and before.state == after.state
        and before.candidates == after.candidates
        and before.index_generation_after == after.index_generation_before
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_proof_manifest(snapshot_path: Path, *, snapshot_id: str) -> list[_ProofEntry]:
    with contextlib.closing(
        sqlite3.connect(f"{snapshot_path.resolve(strict=True).as_uri()}?mode=ro", uri=True)
    ) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT relative_path, path_key, git_population, proof_class,
                      evidence_kind, stat_signature_json, content_hash
               FROM proof_manifest
               WHERE snapshot_id = ?
               ORDER BY path_key""",
            (snapshot_id,),
        ).fetchall()
    entries: list[_ProofEntry] = []
    for row in rows:
        if row["proof_class"] not in _PROOF_CLASSES:
            raise ValueError("unknown proof class")
        signature_raw = (
            json.loads(row["stat_signature_json"])
            if row["stat_signature_json"] is not None
            else None
        )
        signature: tuple[int, int, int, int, int] | None
        if signature_raw is None:
            signature = None
        elif (
            isinstance(signature_raw, list)
            and len(signature_raw) == 5
            and all(
                isinstance(value, int) and not isinstance(value, bool) for value in signature_raw
            )
        ):
            signature = tuple(signature_raw)  # type: ignore[assignment]
        else:
            raise ValueError("invalid proof stat signature")
        entries.append(
            _ProofEntry(
                relative_path=row["relative_path"],
                path_key=row["path_key"],
                git_population=row["git_population"],
                proof_class=row["proof_class"],
                evidence_kind=row["evidence_kind"],
                stat_signature=signature,
                content_hash=row["content_hash"],
            )
        )
    return entries


def _policy_from_meta(snapshot_meta: dict[str, Any]) -> ScanPolicy:
    raw = json.loads(snapshot_meta["policy_json"])
    return ScanPolicy(
        max_text_bytes=int(raw["max_text_bytes"]),
        binary_probe_bytes=int(raw["binary_probe_bytes"]),
        policy_version=str(raw["policy_version"]),
        discovered_metadata_paths=tuple(str(item) for item in raw["discovered_metadata_paths"]),
        discovered_pruned_roots=tuple(str(item) for item in raw["discovered_pruned_roots"]),
    )


def _stored_repo_state(snapshot_meta: dict[str, Any]) -> RepoState:
    raw = json.loads(snapshot_meta["repo_state_after_json"])
    return RepoState(
        head=raw["head"],
        branch=raw["branch"],
        status_fingerprint=raw["status_fingerprint"],
        candidate_fingerprint=raw["candidate_fingerprint"],
        visibility_flags_before=tuple(raw["visibility_flags_before"]),
        index_visibility_paths=tuple(raw["index_visibility_paths"]),
    )


def _active_binding(
    callback: Callable[[], RepositoryBindingObservation] | None,
    expected: tuple[str, str, str],
) -> RepositoryBindingObservation:
    if callback is not None:
        return callback()
    return RepositoryBindingObservation(
        repo_root_norm=expected[0],
        repository_identity_hash=expected[1],
        repository_binding_generation=expected[2],
        live_identity_token=expected[1],
        live_identity_matches=True,
        live_identity_reason="identity_observer_not_required",
    )


def _binding_status(
    observation: RepositoryBindingObservation,
    expected: tuple[str, str, str],
) -> str:
    if observation.registry_binding != expected:
        return "BINDING_MISMATCH"
    if not observation.live_identity_matches:
        return "IDENTITY_MISMATCH"
    return "MATCHED"


def _binding_observations_stable(
    before: RepositoryBindingObservation,
    after: RepositoryBindingObservation,
    expected: tuple[str, str, str],
) -> bool:
    return (
        before == after
        and _binding_status(before, expected) == "MATCHED"
        and _binding_status(after, expected) == "MATCHED"
    )


def _candidate_map(
    candidates: list[Candidate] | tuple[Candidate, ...],
) -> dict[str, tuple[str, str]]:
    return {
        path_key(candidate.relative_path): (candidate.relative_path, candidate.population)
        for candidate in candidates
    }


def _without_pruned_descendants(
    candidates: dict[str, tuple[str, str]],
    pruned_roots: tuple[str, ...],
) -> dict[str, tuple[str, str]]:
    root_keys = {path_key(root) for root in pruned_roots}
    prefixes = tuple(f"{path_key(root.rstrip('/'))}/" for root in pruned_roots)
    return {
        key: value
        for key, value in candidates.items()
        if key in root_keys or not key.startswith(prefixes)
    }


def _repository_deltas(
    stored_state: RepoState,
    current_state: RepoState,
    expected_candidates: dict[str, tuple[str, str]],
    current_candidates: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    if current_state.head != stored_state.head:
        deltas.append(
            {
                "kind": "HEAD_CHANGED",
                "before": stored_state.head,
                "after": current_state.head,
            }
        )
    all_keys = sorted(set(expected_candidates) | set(current_candidates))
    for key in all_keys:
        expected = expected_candidates.get(key)
        current = current_candidates.get(key)
        if expected is None and current is not None:
            deltas.append(
                {
                    "kind": "CANDIDATE_ADDED",
                    "path": current[0],
                    "after_population": current[1],
                }
            )
        elif expected is not None and current is None:
            deltas.append(
                {
                    "kind": "CANDIDATE_REMOVED",
                    "path": expected[0],
                    "before_population": expected[1],
                }
            )
        elif expected is not None and current is not None and expected != current:
            deltas.append(
                {
                    "kind": "CANDIDATE_POPULATION_CHANGED",
                    "path": current[0],
                    "before_population": expected[1],
                    "after_population": current[1],
                }
            )
    return deltas


def _strong_object_deltas(
    repo_root: Path,
    proof_entries: list[_ProofEntry],
    policy: ScanPolicy,
) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for entry in proof_entries:
        if entry.proof_class in {"EXCLUDED_HARD_SECRET", "EXCLUDED_PRUNED"}:
            continue
        matches, reason = compare_persisted_evidence(
            repo_root,
            entry.evidence(),
            policy,
            content_semantics=True,
        )
        if not matches:
            deltas.append(
                {
                    "kind": "OBJECT_PROOF_CHANGED",
                    "path": entry.relative_path,
                    "proof_class": entry.proof_class,
                    "reason": reason,
                }
            )
    return deltas


def _proof_diagnostics(
    stored_state: RepoState,
    current_state: RepoState,
    *,
    mode: VerificationMode,
    exclusions: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    diagnostics: list[dict[str, Any]] = []
    if stored_state.branch != current_state.branch and stored_state.head == current_state.head:
        diagnostics.append(
            {
                "kind": "BRANCH_CHANGED_SAME_HEAD",
                "before": stored_state.branch,
                "after": current_state.branch,
            }
        )
    if (
        mode is VerificationMode.STRONG
        and stored_state.status_fingerprint != current_state.status_fingerprint
    ):
        diagnostics.append(
            {
                "kind": "GIT_STATUS_OUTSIDE_STRONG_SEMANTIC_STALE_PREDICATE",
                "before": stored_state.status_fingerprint,
                "after": current_state.status_fingerprint,
            }
        )
    if mode is VerificationMode.STRONG and exclusions:
        diagnostics.append(
            {
                "kind": "STRONG_PROOF_EXCLUSIONS",
                "paths": list(exclusions),
                "content_verified": False,
            }
        )
    return tuple(diagnostics)
