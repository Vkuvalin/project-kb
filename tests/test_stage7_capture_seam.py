import ast
import contextlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from _git_support import git

from project_kb.errors import IndexingError, ProjectKbError, SnapshotQueryError
from project_kb.indexing import capture as capture_module
from project_kb.indexing import service as service_module
from project_kb.indexing.capture import (
    CaptureAttempt,
    CaptureContract,
    CaptureRequest,
    CaptureRetryEvent,
    CaptureWorkspace,
    SealedArtifactDescriptor,
)
from project_kb.indexing.models import ScanPolicy
from project_kb.indexing.scanner import RepositoryChangedError, ScanError
from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.registry.db import registry_path
from project_kb.resolver.repo_identity import repository_identity_hash
from project_kb.snapshot.database import validate_snapshot


def _lifecycle_counts() -> dict[str, int]:
    with contextlib.closing(sqlite3.connect(registry_path())) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "lifecycle_tasks",
                "lifecycle_operations",
                "snapshot_generations",
                "managed_pointers",
            )
        }


def _request(
    project: object,
    repo_root: Path,
    artifact_path: Path,
) -> CaptureRequest:
    workspace = CaptureWorkspace(
        project_id=project.project_id,  # type: ignore[attr-defined]
        workspace_id=project.workspace_id,  # type: ignore[attr-defined]
        repo_root_norm=project.repo_root_norm,  # type: ignore[attr-defined]
        repository_identity_hash=repository_identity_hash(
            project.repo_root_norm,  # type: ignore[attr-defined]
            project.repo_fingerprint_json,  # type: ignore[attr-defined]
        ),
        binding_generation=project.repo_binding_generation,  # type: ignore[attr-defined]
    )

    index_service = IndexService()

    def workspace_matches_repository(
        actual_root: Path,
        expected: CaptureWorkspace,
    ) -> bool:
        return index_service._active_binding_matches(
            project.project_name,  # type: ignore[attr-defined]
            actual_root,
            expected,
        )

    return CaptureRequest(
        workspace=workspace,
        repository_root=repo_root,
        attempts=(
            CaptureAttempt(
                artifact_path=artifact_path,
                snapshot_id="a" * 32,
                run_id="b" * 32,
            ),
        ),
        contract=CaptureContract.current(ScanPolicy()),
        workspace_matches_repository=workspace_matches_repository,
    )


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm"))


def _run_file_bytes(storage_path: str) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (Path(storage_path) / "runs").iterdir()
        if path.is_file()
    }


def _git_status(repo_root: Path) -> bytes:
    result = git(
        repo_root,
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=all",
    )
    assert isinstance(result.stdout, bytes)
    return result.stdout


def test_capture_seam_returns_closed_artifact_without_publisher_effects(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    (temp_git_repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    project_before = RegistryService().find_project_by_name("repo-one")
    assert project_before is not None
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()
    run_files_before = set((Path(project.storage_path) / "runs").iterdir())
    lifecycle_before = _lifecycle_counts()
    artifact = tmp_path / "explicit-sealed.sqlite"

    descriptor = capture_module.capture_trusted_artifact(
        _request(project_before, temp_git_repo, artifact)
    )

    assert isinstance(descriptor, SealedArtifactDescriptor)
    assert descriptor.artifact_path == artifact
    assert descriptor.workspace.workspace_id == project.workspace_id
    assert descriptor.snapshot_id == "a" * 32
    assert descriptor.run_id == "b" * 32
    assert descriptor.size_bytes == artifact.stat().st_size
    assert descriptor.repository_state_before == descriptor.repository_state_after
    assert descriptor.repository_state_before == descriptor.repository_state_final
    metadata = validate_snapshot(
        artifact,
        project_id=project.project_id,
        expected_policy_version=ScanPolicy().policy_version,
        expected_repo_root_norm=project.repo_root_norm,
        expected_repository_identity_hash=descriptor.workspace.repository_identity_hash,
        expected_repository_binding_generation=project.repo_binding_generation,
    )
    assert metadata["build_status"] == "SEALED"
    assert canonical.read_bytes() == canonical_bytes
    assert RegistryService().find_project_by_name("repo-one") == project_before
    assert set((Path(project.storage_path) / "runs").iterdir()) == run_files_before
    assert _lifecycle_counts() == lifecycle_before
    assert not any(path.exists() for path in _sidecars(artifact))

    renamed = artifact.with_name("renamed-sealed.sqlite")
    artifact.replace(renamed)
    assert renamed.is_file()
    renamed.unlink()
    assert not renamed.exists()


@pytest.mark.parametrize(
    ("phase", "expected_type", "expected_code"),
    [
        ("candidate", ScanError, None),
        ("extraction", ScanError, None),
        ("write", IndexingError, "TEST_WRITE_FAILED"),
        ("validation", SnapshotQueryError, "SNAPSHOT_STORAGE_ERROR"),
        ("final_seal", RepositoryChangedError, None),
        ("descriptor", IndexingError, "TEST_DESCRIPTOR_FAILED"),
    ],
)
def test_capture_failure_isolated_and_cleans_temporary_artifacts(
    temp_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_type: type[BaseException],
    expected_code: str | None,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical.write_bytes(b"previous-canonical")
    project_before = RegistryService().find_project_by_name("repo-one")
    lifecycle_before = _lifecycle_counts()
    artifact = tmp_path / "failed-capture.sqlite"

    if phase == "candidate":
        monkeypatch.setattr(
            capture_module,
            "git_candidates",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ScanError("candidate failure")),
        )
    elif phase == "extraction":
        monkeypatch.setattr(
            capture_module,
            "scan_repository",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ScanError("extract failure")),
        )
    elif phase == "write":
        monkeypatch.setattr(
            capture_module,
            "write_snapshot",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                IndexingError("write failure", code="TEST_WRITE_FAILED")
            ),
        )
    elif phase == "validation":

        def fail_validation(*args: object, **kwargs: object):
            del args, kwargs
            raise SnapshotQueryError("validation failure", code="SNAPSHOT_STORAGE_ERROR")

        monkeypatch.setattr(capture_module, "validate_snapshot", fail_validation)
    elif phase == "final_seal":
        calls = 0
        real_verify = capture_module.verify_scan_evidence

        def fail_terminal_verification(*args: object, **kwargs: object) -> bool:
            nonlocal calls
            calls += 1
            return real_verify(*args, **kwargs) if calls == 1 else False

        monkeypatch.setattr(
            capture_module,
            "verify_scan_evidence",
            fail_terminal_verification,
        )
    else:
        monkeypatch.setattr(
            capture_module,
            "_build_descriptor",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                IndexingError("descriptor failure", code="TEST_DESCRIPTOR_FAILED")
            ),
        )

    with pytest.raises(expected_type) as captured:
        capture_module.capture_trusted_artifact(_request(project, temp_git_repo, artifact))

    if expected_code is not None:
        assert isinstance(captured.value, ProjectKbError)
        assert captured.value.code == expected_code
    assert canonical.read_bytes() == b"previous-canonical"
    assert RegistryService().find_project_by_name("repo-one") == project_before
    assert _lifecycle_counts() == lifecycle_before
    assert not artifact.exists()
    assert not any(path.exists() for path in _sidecars(artifact))


def test_legacy_index_invokes_single_capture_engine_and_no_lifecycle_state(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RegistryService().register("repo-one", temp_git_repo)
    lifecycle_before = _lifecycle_counts()
    real_capture = capture_module.capture_trusted_artifact
    calls = 0

    def count_capture(
        request: CaptureRequest,
        *,
        retry_observer: Callable[[CaptureRetryEvent], None] | None = None,
    ) -> SealedArtifactDescriptor:
        nonlocal calls
        calls += 1
        return real_capture(request, retry_observer=retry_observer)

    monkeypatch.setattr(capture_module, "capture_trusted_artifact", count_capture)

    outcome = IndexService().index("repo-one")

    assert calls == 1
    assert outcome.result == "success"
    assert outcome.data["snapshot"]["truth_claim"] == "CAPTURED_STABLE"
    assert _lifecycle_counts() == lifecycle_before


def test_capture_engine_owns_bounded_repository_change_retry(
    temp_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    request = _request(project, temp_git_repo, tmp_path / "first.sqlite")
    request = replace(
        request,
        attempts=(
            request.attempts[0],
            CaptureAttempt(
                artifact_path=tmp_path / "second.sqlite",
                snapshot_id="c" * 32,
                run_id="d" * 32,
            ),
        ),
    )
    real_scan = capture_module.scan_repository
    scan_calls = 0
    retry_events: list[CaptureRetryEvent] = []

    def first_scan_changes(*args: object, **kwargs: object):
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            raise RepositoryChangedError("first attempt changed")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(capture_module, "scan_repository", first_scan_changes)

    descriptor = capture_module.capture_trusted_artifact(
        request,
        retry_observer=retry_events.append,
    )

    assert scan_calls == 2
    assert [(event.attempt_number, event.error.args[0]) for event in retry_events] == [
        (1, "first attempt changed")
    ]
    assert not request.attempts[0].artifact_path.exists()
    assert descriptor.artifact_path == request.attempts[1].artifact_path
    descriptor.artifact_path.unlink()


def test_legacy_retry_preserves_failed_and_successful_run_artifacts(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    real_scan = capture_module.scan_repository
    scan_calls = 0

    def first_scan_changes(*args: object, **kwargs: object):
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            raise RepositoryChangedError("first legacy attempt changed")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(capture_module, "scan_repository", first_scan_changes)

    outcome = IndexService().index("repo-one")

    run_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (Path(project.storage_path) / "runs").glob("*.json")
    ]
    failed = [record for record in run_records if record["status"] == "FAILED"]
    succeeded = [record for record in run_records if record["status"] == "SUCCESS"]
    assert scan_calls == 2
    assert len(failed) == 1
    assert failed[0]["failure_code"] == "REPO_CHANGED_DURING_SCAN"
    assert failed[0]["details"]["attempt"] == 1
    assert failed[0]["details"]["publication_state"] == "BUILDING"
    assert len(succeeded) == 1
    assert succeeded[0]["run_id"] == outcome.data["index_run"]["run_id"]
    assert succeeded[0]["details"]["snapshot_id"] == outcome.data["snapshot"]["snapshot_id"]


def test_capture_module_dependency_direction_excludes_publishers_and_control_planes() -> None:
    module_path = Path(capture_module.__file__ or "")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = (
        "project_kb.indexing.service",
        "project_kb.cli",
        "project_kb.registry",
        "project_kb.lifecycle",
    )
    assert not any(name.startswith(forbidden) for name in imported)
    assert os.fspath(module_path).endswith(os.fspath(Path("indexing") / "capture.py"))


def test_preflight_preserves_existing_artifact_and_legacy_adapter_skips_cleanup(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()
    run_id = "1" * 32
    existing_artifact = Path(project.storage_path) / "runs" / f"kb-{run_id}.tmp.sqlite"
    existing_artifact.write_bytes(b"unrelated-existing-artifact")
    generated_ids = iter((run_id, "a" * 32, "2" * 32, "b" * 32))
    real_uuid4 = service_module.uuid.uuid4

    def capture_uuid4() -> object:
        try:
            return SimpleNamespace(hex=next(generated_ids))
        except StopIteration:
            return real_uuid4()

    monkeypatch.setattr(
        service_module.uuid,
        "uuid4",
        capture_uuid4,
    )

    def forbidden_cleanup(_path: Path) -> None:
        raise AssertionError("preflight rejection must not call legacy cleanup")

    monkeypatch.setattr(IndexService, "_safe_cleanup", staticmethod(forbidden_cleanup))

    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code == "CAPTURE_DESTINATION_INVALID"
    assert captured.value.retryable is False
    assert existing_artifact.read_bytes() == b"unrelated-existing-artifact"
    assert canonical.read_bytes() == canonical_bytes


def test_preflight_validates_all_attempts_and_preserves_existing_sidecar(
    temp_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    request = _request(project, temp_git_repo, tmp_path / "first.sqlite")
    second = CaptureAttempt(
        artifact_path=tmp_path / "second.sqlite",
        snapshot_id="c" * 32,
        run_id="d" * 32,
    )
    sidecar = Path(f"{second.artifact_path}-wal")
    sidecar.write_bytes(b"unrelated-sidecar")
    request = replace(request, attempts=(request.attempts[0], second))
    monkeypatch.setattr(
        capture_module,
        "_claim_capture_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete preflight must finish before any destination claim")
        ),
    )

    with pytest.raises(IndexingError) as captured:
        capture_module.capture_trusted_artifact(request)

    assert captured.value.code == "CAPTURE_DESTINATION_INVALID"
    assert captured.value.retryable is False
    assert captured.value.details["reason"] == "destination_not_fresh"
    assert not request.attempts[0].artifact_path.exists()
    assert not second.artifact_path.exists()
    assert sidecar.read_bytes() == b"unrelated-sidecar"


def test_preflight_rejects_existing_hard_link_without_touching_either_name(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    source = tmp_path / "unrelated-source.bin"
    source.write_bytes(b"hard-linked-existing-bytes")
    artifact = tmp_path / "hard-linked-attempt.sqlite"
    os.link(source, artifact)

    with pytest.raises(IndexingError) as captured:
        capture_module.capture_trusted_artifact(_request(project, temp_git_repo, artifact))

    assert captured.value.code == "CAPTURE_DESTINATION_INVALID"
    assert source.read_bytes() == b"hard-linked-existing-bytes"
    assert artifact.read_bytes() == b"hard-linked-existing-bytes"


def test_canonical_snapshot_cannot_be_used_as_capture_attempt(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    current = RegistryService().find_project_by_name("repo-one")
    assert current is not None
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()

    with pytest.raises(IndexingError) as captured:
        capture_module.capture_trusted_artifact(_request(current, temp_git_repo, canonical))

    assert captured.value.code == "CAPTURE_DESTINATION_INVALID"
    assert canonical.read_bytes() == canonical_bytes


def test_attempt_path_inside_target_repository_is_rejected_without_repo_drift(
    temp_git_repo: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    artifact = temp_git_repo / "capture-attempt.sqlite"
    status_before = _git_status(temp_git_repo)

    with pytest.raises(IndexingError) as captured:
        capture_module.capture_trusted_artifact(_request(project, temp_git_repo, artifact))

    assert captured.value.code == "CAPTURE_DESTINATION_INVALID"
    assert captured.value.details["reason"] == "destination_inside_repository"
    assert not artifact.exists()
    assert _git_status(temp_git_repo) == status_before


@pytest.mark.parametrize(
    ("duplicate", "expected_code"),
    [
        ("artifact_path", "CAPTURE_DESTINATION_INVALID"),
        ("snapshot_id", "CAPTURE_REQUEST_INVALID"),
        ("run_id", "CAPTURE_REQUEST_INVALID"),
    ],
)
def test_duplicate_attempt_identity_or_path_is_rejected_before_capture(
    temp_git_repo: Path,
    tmp_path: Path,
    duplicate: str,
    expected_code: str,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    request = _request(project, temp_git_repo, tmp_path / "first.sqlite")
    first = request.attempts[0]
    second = CaptureAttempt(
        artifact_path=first.artifact_path
        if duplicate == "artifact_path"
        else tmp_path / "second.sqlite",
        snapshot_id=first.snapshot_id if duplicate == "snapshot_id" else "c" * 32,
        run_id=first.run_id if duplicate == "run_id" else "d" * 32,
    )
    request = replace(request, attempts=(first, second))

    with pytest.raises(IndexingError) as captured:
        capture_module.capture_trusted_artifact(request)

    assert captured.value.code == expected_code
    assert captured.value.retryable is False
    assert not first.artifact_path.exists()
    assert not (tmp_path / "second.sqlite").exists()


def test_normalized_alias_of_same_windows_checkout_is_accepted(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    alias = (
        Path(str(temp_git_repo).swapcase())
        if os.name == "nt"
        else temp_git_repo / ".." / temp_git_repo.name
    )
    artifact = tmp_path / "alias-capture.sqlite"

    descriptor = capture_module.capture_trusted_artifact(_request(project, alias, artifact))

    assert descriptor.artifact_path == artifact
    artifact.unlink()


def test_workspace_from_repo_a_with_actual_repo_b_is_rejected_without_side_effects(
    temp_git_repo: Path,
    second_temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    project_before = RegistryService().find_project_by_name("repo-one")
    assert project_before is not None
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()
    runs_before = _run_file_bytes(project.storage_path)
    lifecycle_before = _lifecycle_counts()
    repo_a_before = _git_status(temp_git_repo)
    repo_b_before = _git_status(second_temp_git_repo)
    artifact = tmp_path / "mismatched-root.sqlite"

    with pytest.raises(RepositoryChangedError):
        capture_module.capture_trusted_artifact(
            _request(project_before, second_temp_git_repo, artifact)
        )

    assert not artifact.exists()
    assert canonical.read_bytes() == canonical_bytes
    assert RegistryService().find_project_by_name("repo-one") == project_before
    assert _run_file_bytes(project.storage_path) == runs_before
    assert _lifecycle_counts() == lifecycle_before
    assert _git_status(temp_git_repo) == repo_a_before
    assert _git_status(second_temp_git_repo) == repo_b_before


def test_live_fingerprint_drift_at_same_path_is_rejected_without_side_effects(
    temp_git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    project_before = RegistryService().find_project_by_name("repo-one")
    assert project_before is not None
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()
    runs_before = _run_file_bytes(project.storage_path)
    lifecycle_before = _lifecycle_counts()
    status_before = _git_status(temp_git_repo)
    real_fingerprint = service_module.build_repository_fingerprint

    def drifted_fingerprint(repo_root: Path):
        fingerprint = real_fingerprint(repo_root)
        return replace(fingerprint, remote_origin_hash="f" * 64)

    monkeypatch.setattr(service_module, "build_repository_fingerprint", drifted_fingerprint)
    artifact = tmp_path / "identity-drift.sqlite"

    with pytest.raises(RepositoryChangedError):
        capture_module.capture_trusted_artifact(_request(project_before, temp_git_repo, artifact))

    assert not artifact.exists()
    assert canonical.read_bytes() == canonical_bytes
    assert RegistryService().find_project_by_name("repo-one") == project_before
    assert _run_file_bytes(project.storage_path) == runs_before
    assert _lifecycle_counts() == lifecycle_before
    assert _git_status(temp_git_repo) == status_before


def test_identity_drift_between_initial_check_and_terminal_seal_is_rejected(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    project_before = RegistryService().find_project_by_name("repo-one")
    assert project_before is not None
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()
    runs_before = _run_file_bytes(project.storage_path)
    lifecycle_before = _lifecycle_counts()
    status_before = _git_status(temp_git_repo)
    request = _request(project_before, temp_git_repo, tmp_path / "terminal-drift.sqlite")
    real_verifier = request.workspace_matches_repository
    verifier_calls = 0

    def drift_after_initial_check(
        actual_root: Path,
        expected: CaptureWorkspace,
    ) -> bool:
        nonlocal verifier_calls
        verifier_calls += 1
        return verifier_calls == 1 and real_verifier(actual_root, expected)

    request = replace(request, workspace_matches_repository=drift_after_initial_check)

    with pytest.raises(RepositoryChangedError):
        capture_module.capture_trusted_artifact(request)

    assert verifier_calls == 2
    assert not request.attempts[0].artifact_path.exists()
    assert canonical.read_bytes() == canonical_bytes
    assert RegistryService().find_project_by_name("repo-one") == project_before
    assert _run_file_bytes(project.storage_path) == runs_before
    assert _lifecycle_counts() == lifecycle_before
    assert _git_status(temp_git_repo) == status_before


def test_legacy_binding_failure_retains_repository_changed_contract(
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_bytes = canonical.read_bytes()
    lifecycle_before = _lifecycle_counts()
    status_before = _git_status(temp_git_repo)
    monkeypatch.setattr(
        IndexService,
        "_active_binding_matches",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(IndexingError) as captured:
        IndexService().index("repo-one")

    assert captured.value.code == "REPO_CHANGED_DURING_SCAN"
    assert canonical.read_bytes() == canonical_bytes
    assert _lifecycle_counts() == lifecycle_before
    assert _git_status(temp_git_repo) == status_before
