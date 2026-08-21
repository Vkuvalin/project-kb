import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from _stage6_support import git
from typer.testing import CliRunner

from project_kb.cli.app import app
from project_kb.indexing.scanner import capture_repo_state, git_candidates
from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.resolver.project import ProjectStatusService

EXPECTED_CURRENTNESS_STATES = {
    "CURRENT",
    "STALE",
    "UNVERIFIED",
    "CHANGED_DURING_CHECK",
    "ERROR",
}
UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS = {
    "can_search": False,
    "can_generate_exports": False,
    "can_generate_context": False,
}
runner = CliRunner()


def _assert_utc_timestamp(value: str | None) -> None:
    assert value is not None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset() is not None


def test_currentness_contract_keeps_all_authoritative_states_distinct() -> None:
    assert len(EXPECTED_CURRENTNESS_STATES) == 5


def test_currentness_result_model_exposes_exact_approved_states() -> None:
    from project_kb.snapshot.currentness import CurrentnessState

    assert {state.value for state in CurrentnessState} == EXPECTED_CURRENTNESS_STATES


@pytest.mark.parametrize(
    ("scenario", "expected", "mismatch_path"),
    [
        ("clean", "CURRENT", None),
        ("tracked_modified", "STALE", "module.py"),
        ("staged_modified", "STALE", "module.py"),
        ("relevant_added", "STALE", "added.py"),
        ("relevant_removed", "STALE", "module.py"),
        ("untracked_unchanged", "CURRENT", None),
        ("untracked_changed", "STALE", "untracked.py"),
        ("ignored_changed", "CURRENT", None),
        ("head_changed", "STALE", None),
        ("branch_changed_same_head", "CURRENT", None),
    ],
)
def test_strong_currentness_mutation_matrix(
    temp_git_repo: Path,
    scenario: str,
    expected: str,
    mismatch_path: str | None,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (temp_git_repo / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py", ".gitignore")
    git(
        temp_git_repo,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Stage 6 baseline",
    )
    if scenario in {"untracked_unchanged", "untracked_changed"}:
        (temp_git_repo / "untracked.py").write_text("VALUE = 1\n", encoding="utf-8")

    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")

    if scenario == "tracked_modified":
        (temp_git_repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif scenario == "staged_modified":
        (temp_git_repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(temp_git_repo, "add", "module.py")
    elif scenario == "relevant_added":
        (temp_git_repo / "added.py").write_text("ADDED = True\n", encoding="utf-8")
    elif scenario == "relevant_removed":
        (temp_git_repo / "module.py").unlink()
    elif scenario == "untracked_changed":
        (temp_git_repo / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif scenario == "ignored_changed":
        (temp_git_repo / "runtime.ignored").write_text("ignored\n", encoding="utf-8")
    elif scenario == "head_changed":
        (temp_git_repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(temp_git_repo, "add", "module.py")
        git(
            temp_git_repo,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Advance HEAD",
        )
    elif scenario == "branch_changed_same_head":
        git(temp_git_repo, "switch", "-c", "stage6-other")

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == expected
    _assert_utc_timestamp(status.snapshot_check.verified_at)
    assert status.snapshot_check.verification_mode == "strong"
    assert status.snapshot_check.verification_scope["included"]
    assert status.snapshot_check.proof_contract_version
    assert status.snapshot_check.verifier_version
    assert status.snapshot_check.truth_claim == (
        "CURRENT_AT_VERIFIED_TIME" if expected == "CURRENT" else "CAPTURED_STABLE"
    )
    if expected == "STALE":
        assert status.snapshot_check.deltas
    if mismatch_path is not None:
        assert mismatch_path in status.snapshot_check.mismatch_paths


def test_removed_fast_is_machine_readable_and_does_not_mutate_state(
    temp_git_repo: Path,
    isolated_kb_home: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")

    snapshot_path = Path(project.storage_path) / "kb.sqlite"
    registry_path = isolated_kb_home / "registry.sqlite"
    runs_path = Path(project.storage_path) / "runs"
    before = {
        "snapshot": snapshot_path.read_bytes(),
        "registry": registry_path.read_bytes(),
        "git_index": (temp_git_repo / ".git" / "index").read_bytes(),
        "git_status": git(
            temp_git_repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout,
        "runs": tuple(
            (path.name, path.read_bytes()) for path in sorted(runs_path.iterdir()) if path.is_file()
        ),
    }

    result = runner.invoke(app, ["status", "repo-one", "--verify", "fast", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["result"] == "blocked"
    assert payload["code"] == "FAST_VERIFICATION_REMOVED"
    assert payload["error"]["code"] == "FAST_VERIFICATION_REMOVED"
    snapshot = payload["data"]["snapshot_check"]
    assert snapshot["status"] == "verification_removed"
    assert snapshot["currentness"] == "UNVERIFIED"
    assert snapshot["verification_mode"] == "fast"
    assert snapshot["truth_claim"] is None
    assert snapshot["verified_at"] is None
    assert snapshot["verification_duration_ms"] is None
    assert snapshot["verification_timings_ms"] == {}
    assert snapshot["verification_attempts"] == 0
    assert "is_current" not in snapshot
    assert payload["data"]["recommended_action"] == {
        "code": "USE_STRONG_VERIFICATION_OR_FULL_CAPTURE",
        "command": "pkb status repo-one --verify strong --json",
        "available": True,
        "requires_user_approval": False,
        "reason": "fast_verification_removed",
    }

    after_runs = tuple(
        (path.name, path.read_bytes()) for path in sorted(runs_path.iterdir()) if path.is_file()
    )
    assert snapshot_path.read_bytes() == before["snapshot"]
    assert registry_path.read_bytes() == before["registry"]
    assert (temp_git_repo / ".git" / "index").read_bytes() == before["git_index"]
    assert (
        git(
            temp_git_repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        == before["git_status"]
    )
    assert after_runs == before["runs"]


def test_currentness_verifier_fast_token_performs_no_proof() -> None:
    from project_kb.snapshot.currentness import verify_snapshot_currentness

    result = verify_snapshot_currentness(
        Path("missing-snapshot.sqlite"),
        repo_root=Path("missing-repository"),
        snapshot_meta={},
        mode="fast",
    )

    assert result.state == "UNVERIFIED"
    assert result.reason == "fast_verification_removed"
    assert result.verified_at is None
    assert result.duration_ms is None
    assert result.timings_ms == {}
    assert result.attempts == 0
    assert result.binding_status == "NOT_CHECKED"


def test_publication_and_unverified_status_expose_captured_stable_truth(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)

    outcome = IndexService().index("repo-one")
    status = ProjectStatusService().status("repo-one")
    query = runner.invoke(app, ["symbols", "repo-one", "--json"])

    assert outcome.data["snapshot"]["truth_claim"] == "CAPTURED_STABLE"
    assert status.snapshot_check.currentness == "UNVERIFIED"
    assert status.snapshot_check.truth_claim == "CAPTURED_STABLE"
    assert status.snapshot_check.verification_mode is None
    assert status.snapshot_check.verified_at is None
    assert status.snapshot_check.verification_timings_ms == {}
    assert status.availability.to_dict() == {
        "can_use_project": True,
        "can_use_snapshot": True,
        **UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS,
    }
    assert query.exit_code == 0
    query_snapshot = json.loads(query.output)["data"]["snapshot"]
    assert query_snapshot["currentness"] == "UNVERIFIED"
    assert query_snapshot["truth_claim"] == "CAPTURED_STABLE"


def test_visibility_neutral_observation_is_stable_and_preserves_real_index(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    from project_kb.indexing.scanner import capture_repo_observation

    index_path = temp_git_repo / ".git" / "index"
    index_bytes_before = index_path.read_bytes()
    index_state_before = git(temp_git_repo, "ls-files", "-v", "-z").stdout

    first = capture_repo_observation(temp_git_repo, temporary_root=tmp_path)
    second = capture_repo_observation(temp_git_repo, temporary_root=tmp_path)

    assert first.sealed is True
    assert second.sealed is True
    assert first.state == second.state
    assert first.candidates == second.candidates
    assert first.index_generation_after == second.index_generation_before
    assert index_path.read_bytes() == index_bytes_before
    assert git(temp_git_repo, "ls-files", "-v", "-z").stdout == index_state_before

    module.write_text("VALUE = 2\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    module.write_text("VALUE = 1\n", encoding="utf-8")
    staged_index_bytes = index_path.read_bytes()
    staged_first = capture_repo_observation(temp_git_repo, temporary_root=tmp_path)
    staged_second = capture_repo_observation(temp_git_repo, temporary_root=tmp_path)

    assert staged_first.sealed is True
    assert staged_second.sealed is True
    assert staged_first.state == staged_second.state
    assert staged_first.candidates == staged_second.candidates
    assert staged_first.index_generation_after == staged_second.index_generation_before
    assert index_path.read_bytes() == staged_index_bytes


def test_status_cli_exposes_strong_currentness_without_unimplemented_features(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")

    result = runner.invoke(app, ["status", "repo-one", "--verify", "strong", "--json"])
    capabilities = runner.invoke(app, ["capabilities", "--json"])

    assert result.exit_code == 0
    assert capabilities.exit_code == 0
    status_data = json.loads(result.output)["data"]
    snapshot = status_data["snapshot_check"]
    assert snapshot["currentness"] == "CURRENT"
    assert snapshot["truth_claim"] == "CURRENT_AT_VERIFIED_TIME"
    assert snapshot["verification_mode"] == "strong"
    assert snapshot["verification_scope"]["included"]
    assert snapshot["proof_contract_version"]
    assert snapshot["verifier_version"]
    _assert_utc_timestamp(snapshot["verified_at"])
    assert "is_current" not in snapshot
    availability = status_data["availability"]
    assert availability == {
        "can_use_project": True,
        "can_use_snapshot": True,
        **UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS,
    }
    capability_features = json.loads(capabilities.output)["data"]["features"]
    reported_feature_flags = {
        "can_search": capability_features["search"],
        "can_generate_exports": capability_features["exports"],
        "can_generate_context": capability_features["context_packs"],
    }
    assert reported_feature_flags == UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS
    assert reported_feature_flags == {
        name: availability[name] for name in UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS
    }

    human = runner.invoke(app, ["status", "repo-one", "--verify", "strong"])
    assert human.exit_code == 0
    assert "Verified at: " in human.output


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_strong_final_terminal_capture_aba_never_returns_current(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_flag: str,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.indexing import scanner as scanner_module
    from project_kb.resolver import project as project_module
    from project_kb.snapshot import currentness as currentness_module

    capture_name = (
        "capture_repo_observation"
        if hasattr(currentness_module, "capture_repo_observation")
        else "capture_repo_state"
    )
    real_capture = getattr(currentness_module, capture_name)
    real_git_bytes = scanner_module._git_bytes
    capture_calls = 0
    target_capture_active = False
    raced = False
    clear_flag = (
        "--no-assume-unchanged" if index_flag == "--assume-unchanged" else "--no-skip-worktree"
    )

    def target_final_capture(*args: object, **kwargs: object):
        nonlocal capture_calls, target_capture_active
        capture_calls += 1
        target_capture_active = capture_calls == 5
        try:
            return real_capture(*args, **kwargs)
        finally:
            target_capture_active = False

    def aba_inside_status(*args: object, **kwargs: object):
        nonlocal raced
        is_status = args[1:] == (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if target_capture_active and is_status and not raced:
            git(temp_git_repo, "update-index", index_flag, "module.py")
            module.write_text("VALUE = 2\n", encoding="utf-8")
            try:
                return real_git_bytes(*args, **kwargs)
            finally:
                git(temp_git_repo, "update-index", clear_flag, "module.py")
                raced = True
        return real_git_bytes(*args, **kwargs)

    monkeypatch.setattr(currentness_module, capture_name, target_final_capture)
    monkeypatch.setattr(scanner_module, "_git_bytes", aba_inside_status)
    real_verify = project_module.verify_snapshot_currentness
    monkeypatch.setattr(
        project_module,
        "verify_snapshot_currentness",
        lambda *args, **kwargs: real_verify(*args, **kwargs, max_attempts=1),
    )

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert raced is True
    assert capture_calls == 5
    assert module.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert status.snapshot_check.currentness != "CURRENT"
    assert status.snapshot_check.currentness in {"STALE", "CHANGED_DURING_CHECK", "UNVERIFIED"}


def test_strong_mode_hashes_dirty_text_when_git_status_fingerprint_is_unchanged(
    temp_git_repo: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    indexed_state = capture_repo_state(temp_git_repo, git_candidates(temp_git_repo))

    module.write_text("VALUE = 3\n", encoding="utf-8")
    current_state = capture_repo_state(temp_git_repo, git_candidates(temp_git_repo))
    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert indexed_state.status_fingerprint == current_state.status_fingerprint
    assert status.snapshot_check.currentness == "STALE"
    assert status.snapshot_check.mismatch_paths == ("module.py",)
    assert status.availability.to_dict() == {
        "can_use_project": True,
        "can_use_snapshot": True,
        **UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS,
    }


def test_strong_mode_staging_only_transition_with_original_worktree_bytes_is_current(
    temp_git_repo: Path,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    module.write_text("VALUE = 1\n", encoding="utf-8")

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"
    assert any(
        item["kind"] == "GIT_STATUS_OUTSIDE_STRONG_SEMANTIC_STALE_PREDICATE"
        for item in status.snapshot_check.diagnostics
    )


def test_strong_mode_tracked_hard_secret_change_is_current_with_explicit_exclusion(
    temp_git_repo: Path,
) -> None:
    secret = temp_git_repo / ".env"
    secret.write_text("TOKEN=before\n", encoding="utf-8")
    git(temp_git_repo, "add", ".env")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    secret.write_text("TOKEN=after!\n", encoding="utf-8")

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"
    assert ".env" in status.snapshot_check.exclusions
    assert any(
        item["kind"] == "STRONG_PROOF_EXCLUSIONS" and item["content_verified"] is False
        for item in status.snapshot_check.diagnostics
    )


def test_strong_mode_tracked_pruned_file_change_is_current_with_explicit_exclusion(
    temp_git_repo: Path,
) -> None:
    pruned = temp_git_repo / ".venv" / "module.py"
    pruned.parent.mkdir()
    pruned.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "-f", ".venv/module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    pruned.write_text("VALUE = 2\n", encoding="utf-8")

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"
    assert ".venv" in status.snapshot_check.exclusions


def test_raw_status_transition_retries_then_returns_current_for_equivalent_strong_scope(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    calls = 0

    def stage_equivalent_transition(*args: object, **kwargs: object):
        nonlocal calls
        result = real_compare(*args, **kwargs)
        calls += 1
        if calls == 1:
            module.write_text("VALUE = 2\n", encoding="utf-8")
            git(temp_git_repo, "add", "module.py")
            module.write_text("VALUE = 1\n", encoding="utf-8")
        return result

    monkeypatch.setattr(currentness_module, "_strong_object_deltas", stage_equivalent_transition)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"
    assert status.snapshot_check.verification_attempts == 2


def test_repeated_raw_status_instability_is_changed_during_check_not_guessed_current(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    calls = 0

    def toggle_staging_after_proof(*args: object, **kwargs: object):
        nonlocal calls
        result = real_compare(*args, **kwargs)
        calls += 1
        if calls == 1:
            module.write_text("VALUE = 2\n", encoding="utf-8")
            git(temp_git_repo, "add", "module.py")
            module.write_text("VALUE = 1\n", encoding="utf-8")
        elif calls == 2:
            git(temp_git_repo, "add", "module.py")
        return result

    monkeypatch.setattr(currentness_module, "_strong_object_deltas", toggle_staging_after_proof)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CHANGED_DURING_CHECK"
    assert status.snapshot_check.verification_attempts == 2


def test_repeatedly_unstable_observation_is_changed_during_check_after_one_retry(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_capture = currentness_module.capture_repo_observation
    calls = 0

    def unstable_capture(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        observation = real_capture(*args, **kwargs)
        return replace(
            observation,
            state=replace(observation.state, branch=f"unstable-{calls}"),
        )

    monkeypatch.setattr(currentness_module, "capture_repo_observation", unstable_capture)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert calls == 4
    assert status.snapshot_check.currentness == "CHANGED_DURING_CHECK"
    assert status.snapshot_check.verification_attempts == 2
    _assert_utc_timestamp(status.snapshot_check.verified_at)


def test_dirty_same_status_mutation_after_first_proof_pass_retries_then_reports_stale(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    calls = 0

    def mutate_after_first_proof(*args: object, **kwargs: object):
        nonlocal calls
        result = real_compare(*args, **kwargs)
        calls += 1
        if calls == 1:
            module.write_text("VALUE = 3\n", encoding="utf-8")
        return result

    monkeypatch.setattr(currentness_module, "_strong_object_deltas", mutate_after_first_proof)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert calls == 3
    assert status.snapshot_check.currentness == "STALE"
    assert status.snapshot_check.verification_attempts == 2
    assert status.snapshot_check.mismatch_paths == ("module.py",)


def test_dirty_same_status_mutation_after_seal_never_returns_current(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    calls = 0

    def mutate_after_seal(*args: object, **kwargs: object):
        nonlocal calls
        result = real_compare(*args, **kwargs)
        calls += 1
        if calls == 2:
            module.write_text("VALUE = 3\n", encoding="utf-8")
        return result

    monkeypatch.setattr(currentness_module, "_strong_object_deltas", mutate_after_seal)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert calls == 3
    assert status.snapshot_check.currentness == "STALE"
    assert status.snapshot_check.mismatch_paths == ("module.py",)


def test_candidate_added_after_seal_is_caught_by_terminal_observation(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_status_paths = currentness_module.git_status_paths
    mutated = False

    def add_candidate_before_terminal(*args: object, **kwargs: object):
        nonlocal mutated
        if not mutated:
            (temp_git_repo / "late.py").write_text("LATE = True\n", encoding="utf-8")
            mutated = True
        return real_status_paths(*args, **kwargs)

    monkeypatch.setattr(currentness_module, "git_status_paths", add_candidate_before_terminal)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "STALE"
    assert status.snapshot_check.mismatch_paths == ("late.py",)


def test_clean_commit_after_seal_is_caught_by_terminal_observation(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_status_paths = currentness_module.git_status_paths
    committed = False

    def commit_candidate_before_terminal(*args: object, **kwargs: object):
        nonlocal committed
        if not committed:
            (temp_git_repo / "late.py").write_text("LATE = True\n", encoding="utf-8")
            git(temp_git_repo, "add", "late.py")
            git(
                temp_git_repo,
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "late candidate",
            )
            committed = True
        return real_status_paths(*args, **kwargs)

    monkeypatch.setattr(
        currentness_module,
        "git_status_paths",
        commit_candidate_before_terminal,
    )

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "STALE"
    assert status.snapshot_check.mismatch_paths == ("late.py",)


def test_dirty_to_clean_after_seal_is_caught_by_terminal_observation(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = temp_git_repo / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "add", "module.py")
    git(temp_git_repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_status_paths = currentness_module.git_status_paths
    restored = False

    def restore_before_terminal(*args: object, **kwargs: object):
        nonlocal restored
        if not restored:
            module.write_text("VALUE = 1\n", encoding="utf-8")
            restored = True
        return real_status_paths(*args, **kwargs)

    monkeypatch.setattr(currentness_module, "git_status_paths", restore_before_terminal)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "STALE"
    assert status.snapshot_check.mismatch_paths == ("module.py",)


def test_same_root_relink_during_verification_never_returns_current(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    relinked = False

    def relink_after_proof(*args: object, **kwargs: object):
        nonlocal relinked
        result = real_compare(*args, **kwargs)
        if not relinked:
            RegistryService().relink("repo-one", temp_git_repo)
            relinked = True
        return result

    monkeypatch.setattr(currentness_module, "_strong_object_deltas", relink_after_proof)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")
    after = ProjectStatusService().status("repo-one")

    assert status.snapshot_check.currentness == "UNVERIFIED"
    assert status.project_state == "SNAPSHOT_REBUILD_REQUIRED"
    assert status.availability.can_use_snapshot is False
    assert {
        name: getattr(status.availability, name)
        for name in UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS
    } == UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS
    assert after.project_state == "SNAPSHOT_REBUILD_REQUIRED"
    assert after.availability.can_use_snapshot is False
    assert {
        name: getattr(after.availability, name) for name in UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS
    } == UNIMPLEMENTED_PUBLIC_FEATURE_FLAGS


def test_one_unstable_proof_attempt_is_discarded_before_current_is_returned(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.indexing.scanner import RepositoryChangedError
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    calls = 0

    def change_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RepositoryChangedError("induced first-attempt race")
        return real_compare(*args, **kwargs)

    monkeypatch.setattr(currentness_module, "_strong_object_deltas", change_once)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert calls == 4
    assert status.snapshot_check.currentness == "CURRENT"
    assert status.snapshot_check.verification_attempts == 2


def test_preexisting_remote_identity_mismatch_prevents_verification_current(
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "remote", "add", "origin", "https://example.invalid/original.git")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    git(temp_git_repo, "remote", "set-url", "origin", "https://example.invalid/replaced.git")

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.project_state == "REPO_MISMATCH"
    assert status.snapshot_check.currentness == "UNVERIFIED"
    assert status.snapshot_check.verified_at is None
    assert status.repo_check.reason == "remote_origin_hash_changed"


def test_remote_identity_change_during_strong_verification_cannot_return_current(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(temp_git_repo, "remote", "add", "origin", "https://example.invalid/original.git")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.snapshot import currentness as currentness_module

    real_compare = currentness_module._strong_object_deltas
    changed = False

    def change_remote_after_first_proof(*args: object, **kwargs: object):
        nonlocal changed
        result = real_compare(*args, **kwargs)
        if not changed:
            git(
                temp_git_repo,
                "remote",
                "set-url",
                "origin",
                "https://example.invalid/replaced.git",
            )
            changed = True
        return result

    monkeypatch.setattr(
        currentness_module,
        "_strong_object_deltas",
        change_remote_after_first_proof,
    )

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.project_state == "REPO_MISMATCH"
    assert status.snapshot_check.currentness == "UNVERIFIED"
    _assert_utc_timestamp(status.snapshot_check.verified_at)


def test_live_identity_changes_then_returns_to_original_retries_before_current(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    service = ProjectStatusService()
    stable = service._repository_binding_observation(project)
    tokens = iter(["identity-a", "identity-b", "identity-a", "identity-a", "identity-a"])

    def changing_identity(_project: object):
        return replace(stable, live_identity_token=next(tokens, "identity-a"))

    monkeypatch.setattr(service, "_repository_binding_observation", changing_identity)

    status = service.status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CURRENT"
    assert status.snapshot_check.verification_attempts == 2


def test_repeated_live_identity_instability_is_changed_during_check(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    service = ProjectStatusService()
    stable = service._repository_binding_observation(project)
    tokens = iter(["identity-a", "identity-b", "identity-a", "identity-b"])

    def unstable_identity(_project: object):
        return replace(stable, live_identity_token=next(tokens, "identity-b"))

    monkeypatch.setattr(service, "_repository_binding_observation", unstable_identity)

    status = service.status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "CHANGED_DURING_CHECK"
    assert status.snapshot_check.verification_attempts == 2


def test_verification_error_is_not_reported_as_stale(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    RegistryService().register("repo-one", temp_git_repo)
    IndexService().index("repo-one")
    from project_kb.indexing.scanner import ScanError
    from project_kb.snapshot import currentness as currentness_module

    def fail_observation(*args: object, **kwargs: object):
        del args, kwargs
        raise ScanError("induced verification failure")

    monkeypatch.setattr(currentness_module, "capture_repo_observation", fail_observation)

    status = ProjectStatusService().status("repo-one", verification_mode="strong")

    assert status.snapshot_check.currentness == "ERROR"
    assert status.project_state == "SNAPSHOT_VERIFICATION_ERROR"
    _assert_utc_timestamp(status.snapshot_check.verified_at)
