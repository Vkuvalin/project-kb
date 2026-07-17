import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _git_support import git

from project_kb.errors import LifecycleOperationError, RegistryOperationError
from project_kb.indexing.capture import (
    CaptureAttempt,
    CaptureContract,
    CaptureRequest,
    CaptureWorkspace,
    SealedArtifactDescriptor,
    capture_trusted_artifact,
)
from project_kb.indexing.models import ScanPolicy
from project_kb.indexing.service import IndexService
from project_kb.lifecycle import generation as generation_module
from project_kb.lifecycle.generation import (
    GenerationReservationRequest,
    LifecycleGenerationService,
    OperationLease,
    OperationReservation,
    PointerExpectation,
    capture_contract_fingerprint,
)
from project_kb.registry import RegistryService
from project_kb.registry.db import registry_path
from project_kb.registry.models import ProjectRecord
from project_kb.storage.home import (
    managed_generation_paths,
    prepare_managed_generation_storage,
    require_managed_regular_file,
    resolve_managed_generation_file,
)

TASK_ID = "a" * 32
BASELINE_POINTER_ID = "b" * 32
WORKING_POINTER_ID = "c" * 32
TIMESTAMP = "2026-07-17T00:00:00Z"
pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Slice 7B2 create-once publication is proven only on the Windows boundary.",
)


@dataclass
class _Harness:
    home: Path
    repo: Path
    project: ProjectRecord
    contract: CaptureContract
    request: GenerationReservationRequest
    lease: OperationLease
    service: LifecycleGenerationService


class _InjectedCrash(BaseException):
    pass


class _CrashOnce:
    def __init__(self, target: str) -> None:
        self.target = target
        self.raised = False

    def __call__(self, checkpoint: str) -> None:
        if checkpoint == self.target and not self.raised:
            self.raised = True
            raise _InjectedCrash(checkpoint)


def test_managed_generation_layout_is_canonical_and_rejects_noncanonical_leaves(
    isolated_kb_home: Path,
) -> None:
    paths = managed_generation_paths(
        isolated_kb_home,
        snapshot_id="d" * 32,
        operation_id="e" * 32,
    )

    assert paths.relative_final_path == f"generations/dd/{'d' * 32}.sqlite"
    assert paths.relative_temp_path == (f"generations/dd/.{'d' * 32}.{'e' * 32}.tmp.sqlite")
    assert paths.final_path == isolated_kb_home.resolve() / paths.relative_final_path

    prepare_managed_generation_storage(paths)
    paths.final_path.mkdir()
    with pytest.raises(RegistryOperationError):
        require_managed_regular_file(paths.final_path, generation_root=paths.generation_root)
    with pytest.raises(RegistryOperationError):
        resolve_managed_generation_file(
            isolated_kb_home,
            snapshot_id="d" * 32,
            relative_storage_path="generations/escape.sqlite",
        )
    with pytest.raises(RegistryOperationError):
        managed_generation_paths(
            isolated_kb_home,
            snapshot_id="../not-an-id",
            operation_id="e" * 32,
        )
    with pytest.raises(LifecycleOperationError) as overlap:
        generation_module._require_generation_storage_isolated(
            paths,
            repository_root=str(isolated_kb_home.parent),
        )
    assert overlap.value.code == "GENERATION_STORAGE_OVERLAP"


def test_publish_register_pointer_and_replay_are_file_first_and_immutable(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    observed: dict[str, Any] = {}

    def checkpoint(name: str) -> None:
        if name == "after_file_publication":
            reservation = observed["reservation"]
            assert reservation.paths.final_path.is_file()
            with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
                observed["after_file_counts"] = _lifecycle_counts(conn)

    harness = _setup_harness(
        isolated_kb_home,
        temp_git_repo,
        checkpoint=checkpoint,
    )
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    observed["reservation"] = reservation
    reservation, descriptor = _capture(harness, reservation)
    result = harness.service.publish_artifact(reservation, descriptor)

    assert observed["after_file_counts"]["snapshot_generations"] == 0
    assert observed["after_file_counts"]["managed_pointers"] == 0
    assert result.operation_id == reservation.operation_id
    assert result.snapshot_id == reservation.snapshot_id
    assert result.generation_sequence == 0
    assert result.generation_state == "AVAILABLE"
    assert result.pointer_versions == (("TASK_BASELINE", 0),)
    assert not result.replayed
    assert not reservation.paths.temp_path.exists()
    assert reservation.paths.final_path.is_file()
    assert result.file_size == reservation.paths.final_path.stat().st_size
    assert result.file_sha256 == _sha256(reservation.paths.final_path)
    assert not any(
        os.path.lexists(path)
        for path in (reservation.paths.temp_path, *_sidecars(reservation.paths.temp_path))
    )
    assert not any(os.path.lexists(path) for path in _sidecars(reservation.paths.final_path))
    published_bytes = reservation.paths.final_path.read_bytes()

    exact = harness.service.resolve_generation(result.snapshot_id)
    replay = harness.service.recover_operation(harness.request, harness.lease)

    assert exact.path == reservation.paths.final_path
    assert exact.file_sha256 == result.file_sha256
    assert replay.operation_id == result.operation_id
    assert replay.snapshot_id == result.snapshot_id
    assert replay.replayed
    assert not any(
        os.path.lexists(path)
        for path in (reservation.paths.temp_path, *_sidecars(reservation.paths.temp_path))
    )
    assert reservation.paths.final_path.read_bytes() == published_bytes
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        operation = conn.execute(
            "SELECT * FROM lifecycle_operations WHERE operation_id = ?",
            (result.operation_id,),
        ).fetchone()
        generation = conn.execute(
            "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
            (result.snapshot_id,),
        ).fetchone()
        pointer = conn.execute(
            "SELECT * FROM managed_pointers WHERE pointer_id = ?",
            (BASELINE_POINTER_ID,),
        ).fetchone()
    assert operation is not None and operation[4] == "COMMITTED"
    assert generation is not None and generation[11] == result.relative_storage_path
    assert pointer is not None and pointer[4] == result.snapshot_id


def test_reservation_idempotency_active_lease_and_single_unresolved_operation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    first = harness.service.reserve_operation(harness.request, harness.lease)
    replay = harness.service.reserve_operation(harness.request, harness.lease)

    assert replay.operation_id == first.operation_id
    assert replay.snapshot_id == first.snapshot_id
    assert replay.generation_sequence == first.generation_sequence

    with pytest.raises(LifecycleOperationError) as different:
        harness.service.reserve_operation(
            replace(harness.request, expected_branch="different-branch"),
            harness.lease,
        )
    assert different.value.code == "IDEMPOTENCY_KEY_REUSED"

    with pytest.raises(LifecycleOperationError) as active:
        harness.service.reserve_operation(
            harness.request,
            OperationLease(owner="other-owner", token="other-token"),
        )
    assert active.value.code == "OPERATION_IN_PROGRESS"
    assert active.value.details["operation_id"] == first.operation_id

    second_request = replace(
        harness.request,
        idempotency_key="second-operation",
        expected_task_version=1,
        pointer_expectations=(
            replace(harness.request.pointer_expectations[0], pointer_id="d" * 32),
        ),
    )
    with pytest.raises(LifecycleOperationError) as unresolved:
        harness.service.reserve_operation(second_request, harness.lease)
    assert unresolved.value.code == "OPERATION_IN_PROGRESS"

    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        counts = _lifecycle_counts(conn)
        task = conn.execute(
            "SELECT next_generation_sequence, row_version FROM lifecycle_tasks WHERE task_id = ?",
            (TASK_ID,),
        ).fetchone()
    assert counts["lifecycle_operations"] == 1
    assert task == (1, 1)


def test_expired_incomplete_build_is_cleaned_and_sequence_is_not_reused(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    now = [datetime(2026, 7, 17, tzinfo=UTC)]
    harness = _setup_harness(
        isolated_kb_home,
        temp_git_repo,
        clock=lambda: now[0],
        lease=OperationLease(owner="builder", token="lease-one", duration_seconds=1),
    )
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation = harness.service.begin_build(reservation)
    reservation.paths.temp_path.write_bytes(b"incomplete-sqlite")
    now[0] += timedelta(seconds=2)
    recovery_lease = OperationLease(owner="recovery", token="lease-two", duration_seconds=30)

    with pytest.raises(LifecycleOperationError) as failed:
        harness.service.recover_operation(harness.request, recovery_lease)

    assert failed.value.code == "OPERATION_BUILD_INCOMPLETE"
    assert not reservation.paths.temp_path.exists()
    assert not reservation.paths.final_path.exists()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        operation = conn.execute(
            "SELECT operation_id, operation_phase FROM lifecycle_operations"
        ).fetchone()
        task = conn.execute(
            "SELECT next_generation_sequence FROM lifecycle_tasks WHERE task_id = ?",
            (TASK_ID,),
        ).fetchone()
    assert operation == (reservation.operation_id, "FAILED")
    assert task == (1,)

    same = harness.service.reserve_operation(harness.request, recovery_lease)
    assert same.operation_id == reservation.operation_id
    assert same.snapshot_id == reservation.snapshot_id
    assert same.operation_phase == "FAILED"


def test_expired_sealed_temp_resumes_the_original_operation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    now = [datetime(2026, 7, 17, tzinfo=UTC)]
    harness = _setup_harness(
        isolated_kb_home,
        temp_git_repo,
        clock=lambda: now[0],
        lease=OperationLease(owner="builder", token="lease-one", duration_seconds=1),
    )
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, _descriptor = _capture(harness, reservation)
    now[0] += timedelta(seconds=2)

    recovered = harness.service.recover_operation(
        harness.request,
        harness.lease,
    )

    assert recovered.operation_id == reservation.operation_id
    assert recovered.snapshot_id == reservation.snapshot_id
    assert recovered.generation_state == "AVAILABLE"
    assert not reservation.paths.temp_path.exists()
    assert reservation.paths.final_path.is_file()


@pytest.mark.parametrize("checkpoint_name", ["after_file_publication", "after_registry_commit"])
def test_crash_windows_recover_without_duplicate_operation_or_generation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    checkpoint_name: str,
) -> None:
    crash = _CrashOnce(checkpoint_name)
    harness = _setup_harness(
        isolated_kb_home,
        temp_git_repo,
        checkpoint=crash,
    )
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)

    with pytest.raises(_InjectedCrash):
        harness.service.publish_artifact(reservation, descriptor)

    recovery = LifecycleGenerationService(home=isolated_kb_home)
    result = recovery.recover_operation(
        harness.request,
        harness.lease,
    )

    assert result.operation_id == reservation.operation_id
    assert result.snapshot_id == reservation.snapshot_id
    assert result.replayed == (checkpoint_name == "after_registry_commit")
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn) == {
            "lifecycle_tasks": 1,
            "lifecycle_operations": 1,
            "snapshot_generations": 1,
            "managed_pointers": 1,
        }


def test_losing_finalizer_replays_the_same_committed_operation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    observed: dict[str, Any] = {}

    def finalize_elsewhere(checkpoint: str) -> None:
        if checkpoint == "before_registry_finalize" and "other" not in observed:
            observed["other"] = LifecycleGenerationService(home=isolated_kb_home).recover_operation(
                observed["request"], observed["lease"]
            )

    harness = _setup_harness(
        isolated_kb_home,
        temp_git_repo,
        checkpoint=finalize_elsewhere,
    )
    observed["request"] = harness.request
    observed["lease"] = harness.lease
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)

    losing_result = harness.service.publish_artifact(reservation, descriptor)

    assert observed["other"].operation_id == reservation.operation_id
    assert losing_result.operation_id == reservation.operation_id
    assert losing_result.replayed
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        states = conn.execute("SELECT generation_state FROM snapshot_generations").fetchall()
    assert states == [("AVAILABLE",)]


def test_existing_unproven_destination_is_never_overwritten_and_requires_recovery(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    reservation.paths.final_path.write_bytes(b"foreign-existing-destination")
    before = reservation.paths.final_path.read_bytes()

    with pytest.raises(LifecycleOperationError) as collision:
        harness.service.publish_artifact(reservation, descriptor)

    assert collision.value.code == "PUBLICATION_COLLISION"
    assert reservation.paths.final_path.read_bytes() == before
    assert reservation.paths.temp_path.is_file()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        operation = conn.execute(
            "SELECT operation_phase FROM lifecycle_operations WHERE operation_id = ?",
            (reservation.operation_id,),
        ).fetchone()
        assert _lifecycle_counts(conn)["snapshot_generations"] == 0
    assert operation == ("RECOVERY_REQUIRED",)


def test_exact_matching_destination_is_an_idempotent_publication_candidate(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    exact_bytes = reservation.paths.temp_path.read_bytes()
    reservation.paths.final_path.write_bytes(exact_bytes)

    result = harness.service.publish_artifact(reservation, descriptor)

    assert result.generation_state == "AVAILABLE"
    assert not reservation.paths.temp_path.exists()
    assert reservation.paths.final_path.read_bytes() == exact_bytes


def test_matching_destination_locked_duplicate_temp_blocks_commit_until_released(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation, exact_bytes, temp_locked = _prepare_locked_matching_duplicate(
        harness,
        monkeypatch,
    )

    with pytest.raises(LifecycleOperationError) as locked_recovery:
        harness.service.recover_operation(harness.request, harness.lease)

    assert locked_recovery.value.code == "PUBLICATION_SHARING_VIOLATION"
    assert locked_recovery.value.retryable
    assert reservation.paths.final_path.read_bytes() == exact_bytes
    assert reservation.paths.temp_path.read_bytes() == exact_bytes
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn)["snapshot_generations"] == 0
        assert _lifecycle_counts(conn)["managed_pointers"] == 0
        operation = conn.execute(
            """SELECT operation_phase, failure_json FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
    assert operation is not None and operation[0] == "RECOVERY_REQUIRED"
    assert json.loads(operation[1])["code"] == "DUPLICATE_TEMP_CLEANUP_BLOCKED"

    temp_locked["active"] = False
    recovered = harness.service.recover_operation(harness.request, harness.lease)

    assert recovered.operation_id == reservation.operation_id
    assert recovered.snapshot_id == reservation.snapshot_id
    assert not recovered.replayed
    assert reservation.paths.final_path.read_bytes() == exact_bytes
    assert not any(
        os.path.lexists(path)
        for path in (reservation.paths.temp_path, *_sidecars(reservation.paths.temp_path))
    )
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn)["snapshot_generations"] == 1
        assert _lifecycle_counts(conn)["managed_pointers"] == 1
        operation = conn.execute(
            """SELECT operation_phase FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
        pointer = conn.execute(
            """SELECT snapshot_id FROM managed_pointers WHERE pointer_id = ?""",
            (BASELINE_POINTER_ID,),
        ).fetchone()
    assert operation == ("COMMITTED",)
    assert pointer == (reservation.snapshot_id,)


def test_recovery_preserves_mismatched_duplicate_temp_and_fails_closed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation, exact_bytes, temp_locked = _prepare_locked_matching_duplicate(
        harness,
        monkeypatch,
    )
    temp_locked["active"] = False
    with reservation.paths.temp_path.open("ab") as stream:
        stream.write(b"mismatched-duplicate")
    mismatched_bytes = reservation.paths.temp_path.read_bytes()

    with pytest.raises(LifecycleOperationError) as recovery_required:
        harness.service.recover_operation(harness.request, harness.lease)

    assert recovery_required.value.code == "OPERATION_RECOVERY_REQUIRED"
    assert reservation.paths.final_path.read_bytes() == exact_bytes
    assert reservation.paths.temp_path.read_bytes() == mismatched_bytes
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn)["snapshot_generations"] == 0
        assert _lifecycle_counts(conn)["managed_pointers"] == 0
        operation = conn.execute(
            """SELECT operation_phase, failure_json FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
    assert operation is not None and operation[0] == "RECOVERY_REQUIRED"
    assert json.loads(operation[1])["code"] == "DUPLICATE_TEMP_UNSAFE"


@pytest.mark.parametrize("unsafe_evidence", ["hardlink", "sidecar"])
def test_recovery_preserves_unsafe_duplicate_temp_evidence(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_evidence: str,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation, exact_bytes, temp_locked = _prepare_locked_matching_duplicate(
        harness,
        monkeypatch,
    )
    temp_locked["active"] = False
    if unsafe_evidence == "hardlink":
        unsafe_path = isolated_kb_home / "duplicate-temp-alias.sqlite"
        os.link(reservation.paths.temp_path, unsafe_path)
    else:
        unsafe_path = Path(f"{reservation.paths.temp_path}-wal")
        unsafe_path.write_bytes(b"unexpected-sidecar")

    with pytest.raises(LifecycleOperationError) as recovery_required:
        harness.service.recover_operation(harness.request, harness.lease)

    assert recovery_required.value.code == "OPERATION_RECOVERY_REQUIRED"
    assert reservation.paths.final_path.read_bytes() == exact_bytes
    assert reservation.paths.temp_path.read_bytes() == exact_bytes
    assert unsafe_path.exists()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn)["snapshot_generations"] == 0
        assert _lifecycle_counts(conn)["managed_pointers"] == 0
        operation = conn.execute(
            """SELECT operation_phase, failure_json FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
    assert operation is not None and operation[0] == "RECOVERY_REQUIRED"
    assert json.loads(operation[1])["code"] == "DUPLICATE_TEMP_UNSAFE"


def test_sqlite_sidecar_rejects_the_sealed_artifact_before_publication(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    Path(f"{reservation.paths.temp_path}-wal").write_bytes(b"unexpected-sidecar")

    with pytest.raises(LifecycleOperationError) as sidecar:
        harness.service.publish_artifact(reservation, descriptor)

    assert sidecar.value.code == "GENERATION_ARTIFACT_INVALID"
    assert reservation.paths.temp_path.is_file()
    assert not reservation.paths.final_path.exists()


def test_hardlinked_temp_artifact_is_not_publishable_as_immutable_storage(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    alias = isolated_kb_home / "temp-artifact-alias.sqlite"
    os.link(reservation.paths.temp_path, alias)

    with pytest.raises(LifecycleOperationError) as hardlink:
        harness.service.publish_artifact(reservation, descriptor)

    assert hardlink.value.code == "GENERATION_ARTIFACT_INVALID"
    assert alias.read_bytes() == reservation.paths.temp_path.read_bytes()
    assert not reservation.paths.final_path.exists()


def test_existing_nonregular_destination_is_a_collision_not_a_sharing_retry(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    reservation.paths.final_path.mkdir()

    with pytest.raises(LifecycleOperationError) as collision:
        harness.service.publish_artifact(reservation, descriptor)

    assert collision.value.code == "PUBLICATION_COLLISION"
    assert reservation.paths.final_path.is_dir()
    assert reservation.paths.temp_path.is_file()


def test_unprobed_platform_fails_before_the_publication_syscall(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    monkeypatch.setattr(generation_module, "_WINDOWS_RENAME_FAILS_IF_EXISTS", False)

    with pytest.raises(LifecycleOperationError) as unsupported:
        harness.service.publish_artifact(reservation, descriptor)

    assert unsupported.value.code == "PUBLICATION_PLATFORM_UNSUPPORTED"
    assert reservation.paths.temp_path.is_file()
    assert not reservation.paths.final_path.exists()


def test_final_file_without_durable_seal_witness_fails_closed(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, _descriptor = _capture(harness, reservation)
    os.rename(reservation.paths.temp_path, reservation.paths.final_path)

    with pytest.raises(LifecycleOperationError) as unproven:
        harness.service.recover_operation(harness.request, harness.lease)

    assert unproven.value.code == "OPERATION_RECOVERY_REQUIRED"
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        operation = conn.execute(
            "SELECT operation_phase FROM lifecycle_operations WHERE operation_id = ?",
            (reservation.operation_id,),
        ).fetchone()
    assert operation == ("RECOVERY_REQUIRED",)


def test_wrong_descriptor_ownership_fails_before_publication(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    wrong = replace(
        descriptor,
        workspace=replace(descriptor.workspace, project_id="f" * 32),
    )

    with pytest.raises(LifecycleOperationError) as mismatch:
        harness.service.publish_artifact(reservation, wrong)

    assert mismatch.value.code == "GENERATION_ARTIFACT_MISMATCH"
    assert reservation.paths.temp_path.is_file()
    assert not reservation.paths.final_path.exists()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn)["snapshot_generations"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "home",
        "temp_path",
        "final_path",
        "pointer_id",
        "pointer_version",
        "pointer_predecessor",
        "parent",
        "binding",
        "operation_kind",
        "operation_phase",
        "expected_head",
        "expected_branch",
        "capture_contract",
        "repository_root",
        "repository_identity",
        "lease_token",
    ],
)
def test_build_rejects_every_caller_modified_reservation_before_storage_mutation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    field: str,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    before = _registry_state(isolated_kb_home)

    with pytest.raises(LifecycleOperationError) as mismatch:
        harness.service.begin_build(_tamper_reservation(reservation, field))

    assert mismatch.value.code == "OPERATION_IDENTITY_MISMATCH"
    assert _registry_state(isolated_kb_home) == before
    assert not reservation.paths.generation_root.exists()


@pytest.mark.parametrize(
    "field",
    [
        "home",
        "temp_path",
        "final_path",
        "pointer_id",
        "pointer_version",
        "pointer_predecessor",
        "parent",
        "binding",
        "operation_kind",
        "operation_phase",
        "expected_head",
        "expected_branch",
        "capture_contract",
        "repository_root",
        "repository_identity",
        "lease_token",
    ],
)
def test_publication_rejects_every_caller_modified_reservation_without_drift(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    field: str,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    before = _registry_state(isolated_kb_home)
    temp_bytes = reservation.paths.temp_path.read_bytes()

    with pytest.raises(LifecycleOperationError) as mismatch:
        harness.service.publish_artifact(_tamper_reservation(reservation, field), descriptor)

    assert mismatch.value.code == "OPERATION_IDENTITY_MISMATCH"
    assert _registry_state(isolated_kb_home) == before
    assert reservation.paths.temp_path.read_bytes() == temp_bytes
    assert not reservation.paths.final_path.exists()


def test_persisted_request_fingerprint_is_recomputed_before_build_mutation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        plan = json.loads(
            conn.execute(
                """SELECT expected_pointer_versions_json FROM lifecycle_operations
                   WHERE operation_id = ?""",
                (reservation.operation_id,),
            ).fetchone()[0]
        )
        plan["normalized_request"]["expected_branch"] = "corrupt-plan"
        _rewrite_operation_plan(conn, reservation.operation_id, plan)

    with pytest.raises(LifecycleOperationError) as mismatch:
        harness.service.begin_build(reservation)

    assert mismatch.value.code == "OPERATION_IDENTITY_MISMATCH"
    assert not reservation.paths.generation_root.exists()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        phase = conn.execute(
            """SELECT operation_phase FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
    assert phase == ("RESERVED",)


def test_first_and_later_working_require_the_authoritative_pointer_predecessor(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    before_first = _registry_state(isolated_kb_home)

    with pytest.raises(LifecycleOperationError) as wrong_first:
        harness.service.reserve_operation(
            _working_request(
                harness,
                expected_task_version=2,
                parent_snapshot_id="f" * 32,
                expected_pointer_version=None,
                expected_pointer_snapshot=None,
                idempotency_key="wrong-first-parent",
            ),
            harness.lease,
        )

    assert wrong_first.value.code == "GENERATION_LINEAGE_INVALID"
    assert _registry_state(isolated_kb_home) == before_first

    working_one_request = _working_request(
        harness,
        expected_task_version=2,
        parent_snapshot_id=baseline.snapshot_id,
        expected_pointer_version=None,
        expected_pointer_snapshot=None,
        idempotency_key="working-one",
    )
    working_one = _publish_request(harness, working_one_request)
    before_later = _registry_state(isolated_kb_home)

    with pytest.raises(LifecycleOperationError) as wrong_later:
        harness.service.reserve_operation(
            _working_request(
                harness,
                expected_task_version=3,
                parent_snapshot_id=baseline.snapshot_id,
                expected_pointer_version=0,
                expected_pointer_snapshot=working_one.snapshot_id,
                idempotency_key="wrong-later-parent",
            ),
            harness.lease,
        )

    assert wrong_later.value.code == "GENERATION_LINEAGE_INVALID"
    assert _registry_state(isolated_kb_home) == before_later


def test_final_requires_the_exact_latest_working_predecessor(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)

    with pytest.raises(LifecycleOperationError) as missing_latest:
        harness.service.reserve_operation(
            _final_request(
                harness,
                expected_task_version=2,
                parent_snapshot_id=baseline.snapshot_id,
                idempotency_key="final-without-working",
            ),
            harness.lease,
        )
    assert missing_latest.value.code == "GENERATION_LINEAGE_INVALID"

    working_one = _publish_request(
        harness,
        _working_request(
            harness,
            expected_task_version=2,
            parent_snapshot_id=baseline.snapshot_id,
            expected_pointer_version=None,
            expected_pointer_snapshot=None,
            idempotency_key="working-one",
        ),
    )
    with pytest.raises(LifecycleOperationError) as wrong_latest:
        harness.service.reserve_operation(
            _final_request(
                harness,
                expected_task_version=3,
                parent_snapshot_id=baseline.snapshot_id,
                idempotency_key="final-wrong-parent",
            ),
            harness.lease,
        )
    assert wrong_latest.value.code == "GENERATION_LINEAGE_INVALID"

    final_reservation = harness.service.reserve_operation(
        _final_request(
            harness,
            expected_task_version=3,
            parent_snapshot_id=working_one.snapshot_id,
            idempotency_key="final-correct-parent",
        ),
        harness.lease,
    )
    assert final_reservation.parent_snapshot_id == working_one.snapshot_id
    assert final_reservation.predecessor_pointer is not None
    assert final_reservation.predecessor_pointer.pointer_role == "TASK_LATEST_WORKING"
    assert final_reservation.predecessor_pointer.expected_pointer_version == 0


def test_future_latest_working_parent_is_rejected_before_sequence_reservation(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    working_one = _publish_request(
        harness,
        _working_request(
            harness,
            expected_task_version=2,
            parent_snapshot_id=baseline.snapshot_id,
            expected_pointer_version=None,
            expected_pointer_snapshot=None,
            idempotency_key="working-one",
        ),
    )
    future_snapshot_id = "f" * 32
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        prior = conn.execute(
            "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
            (working_one.snapshot_id,),
        ).fetchone()
        assert prior is not None
        _insert_generation_from_row(
            conn,
            prior,
            snapshot_id=future_snapshot_id,
            generation_sequence=99,
            parent_snapshot_id=working_one.snapshot_id,
            relative_path=f"generations/ff/{future_snapshot_id}.sqlite",
        )
        conn.execute(
            """UPDATE managed_pointers
               SET snapshot_id = ?, pointer_version = pointer_version + 1, updated_at = ?
               WHERE pointer_id = ?""",
            (future_snapshot_id, TIMESTAMP, WORKING_POINTER_ID),
        )
    before = _registry_state(isolated_kb_home)

    with pytest.raises(LifecycleOperationError) as future:
        harness.service.reserve_operation(
            _working_request(
                harness,
                expected_task_version=3,
                parent_snapshot_id=future_snapshot_id,
                expected_pointer_version=1,
                expected_pointer_snapshot=future_snapshot_id,
                idempotency_key="future-parent",
            ),
            harness.lease,
        )

    assert future.value.code == "GENERATION_LINEAGE_INVALID"
    assert _registry_state(isolated_kb_home) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "sidecar"])
def test_damaged_live_predecessor_blocks_reservation_without_fallback(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    damage: str,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    baseline_path = harness.service.resolve_generation(baseline.snapshot_id).path
    if damage == "missing":
        baseline_path.unlink()
    elif damage == "corrupt":
        with baseline_path.open("ab") as stream:
            stream.write(b"corruption")
    else:
        Path(f"{baseline_path}-wal").write_bytes(b"sidecar")
    before = _registry_state(isolated_kb_home)

    with pytest.raises(LifecycleOperationError) as broken:
        harness.service.reserve_operation(
            _working_request(
                harness,
                expected_task_version=2,
                parent_snapshot_id=baseline.snapshot_id,
                expected_pointer_version=None,
                expected_pointer_snapshot=None,
                idempotency_key=f"broken-{damage}",
            ),
            harness.lease,
        )

    assert broken.value.code == "BROKEN_POINTER"
    assert broken.value.details["snapshot_id"] == baseline.snapshot_id
    assert _registry_state(isolated_kb_home) == before


def test_working_chain_replay_returns_original_pointer_version_after_advancement(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    working_one_request = _working_request(
        harness,
        expected_task_version=2,
        parent_snapshot_id=baseline.snapshot_id,
        expected_pointer_version=None,
        expected_pointer_snapshot=None,
        idempotency_key="working-one",
    )
    working_one = _publish_request(harness, working_one_request)
    working_two_request = _working_request(
        harness,
        expected_task_version=3,
        parent_snapshot_id=working_one.snapshot_id,
        expected_pointer_version=0,
        expected_pointer_snapshot=working_one.snapshot_id,
        idempotency_key="working-two",
    )
    working_two = _publish_request(harness, working_two_request)

    replay = harness.service.recover_operation(working_one_request, harness.lease)

    assert replay.operation_id == working_one.operation_id
    assert replay.snapshot_id == working_one.snapshot_id
    assert replay.pointer_versions == (("TASK_LATEST_WORKING", 0),)
    assert replay.replayed
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        pointer = conn.execute(
            """SELECT snapshot_id, pointer_version FROM managed_pointers
               WHERE pointer_id = ?""",
            (WORKING_POINTER_ID,),
        ).fetchone()
        lineage = conn.execute(
            """SELECT snapshot_id, generation_sequence, parent_snapshot_id, capture_purpose
               FROM snapshot_generations WHERE task_id = ? ORDER BY generation_sequence""",
            (TASK_ID,),
        ).fetchall()
    assert pointer == (working_two.snapshot_id, 1)
    assert lineage == [
        (baseline.snapshot_id, 0, None, "TASK_BASELINE"),
        (working_one.snapshot_id, 1, baseline.snapshot_id, "TASK_WORKING"),
        (working_two.snapshot_id, 2, working_one.snapshot_id, "TASK_WORKING"),
    ]


def test_committed_replay_requires_consistent_immutable_result_evidence(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        row = conn.execute(
            """SELECT event_id, details_json FROM registry_events
               WHERE event_type = 'lifecycle_generation_committed'"""
        ).fetchone()
        details = json.loads(row[1])
        details["pointer_versions"] = {"TASK_BASELINE": 99}
        conn.execute(
            "UPDATE registry_events SET details_json = ? WHERE event_id = ?",
            (json.dumps(details, sort_keys=True, separators=(",", ":")), row[0]),
        )

    with pytest.raises(LifecycleOperationError) as inconsistent:
        harness.service.recover_operation(harness.request, harness.lease)

    assert inconsistent.value.code == "OPERATION_RECOVERY_REQUIRED"
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        pointer = conn.execute(
            "SELECT snapshot_id, pointer_version FROM managed_pointers WHERE pointer_id = ?",
            (BASELINE_POINTER_ID,),
        ).fetchone()
    assert pointer == (baseline.snapshot_id, 0)


@pytest.mark.parametrize(
    ("conflict_kind", "expected_code"),
    [
        ("pointer", "POINTER_CAS_CONFLICT"),
        ("task", "TASK_VERSION_CONFLICT"),
        ("workspace", "WORKSPACE_BINDING_CONFLICT"),
    ],
)
def test_prepublication_terminal_conflict_fails_and_unblocks_next_sequence(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    conflict_kind: str,
    expected_code: str,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    working_one = _publish_request(
        harness,
        _working_request(
            harness,
            expected_task_version=2,
            parent_snapshot_id=baseline.snapshot_id,
            expected_pointer_version=None,
            expected_pointer_snapshot=None,
            idempotency_key="working-one",
        ),
    )
    stale_request = _working_request(
        harness,
        expected_task_version=3,
        parent_snapshot_id=working_one.snapshot_id,
        expected_pointer_version=0,
        expected_pointer_snapshot=working_one.snapshot_id,
        idempotency_key=f"stale-{conflict_kind}",
    )
    reservation = harness.service.reserve_operation(stale_request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    _introduce_prepublication_conflict(isolated_kb_home, conflict_kind)

    with pytest.raises(LifecycleOperationError) as conflict:
        harness.service.publish_artifact(reservation, descriptor)

    assert conflict.value.code == expected_code
    assert not reservation.paths.temp_path.exists()
    assert not reservation.paths.final_path.exists()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        operation = conn.execute(
            """SELECT operation_phase, failure_json FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
        generation_count = conn.execute("SELECT COUNT(*) FROM snapshot_generations").fetchone()[0]
    assert operation is not None and operation[0] == "FAILED"
    assert json.loads(operation[1])["code"] == expected_code
    assert generation_count == 2

    with pytest.raises(LifecycleOperationError) as replay:
        harness.service.recover_operation(stale_request, harness.lease)
    assert replay.value.code == "OPERATION_FAILED"
    if conflict_kind == "workspace":
        _restore_workspace_active(isolated_kb_home)
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        task_version = conn.execute(
            "SELECT row_version FROM lifecycle_tasks WHERE task_id = ?",
            (TASK_ID,),
        ).fetchone()[0]
        pointer = conn.execute(
            """SELECT snapshot_id, pointer_version FROM managed_pointers
               WHERE pointer_id = ?""",
            (WORKING_POINTER_ID,),
        ).fetchone()
    next_request = _working_request(
        harness,
        expected_task_version=task_version,
        parent_snapshot_id=working_one.snapshot_id,
        expected_pointer_version=pointer[1],
        expected_pointer_snapshot=pointer[0],
        idempotency_key=f"after-{conflict_kind}",
    )
    next_reservation = harness.service.reserve_operation(next_request, harness.lease)
    assert next_reservation.generation_sequence == 3


def test_prepublication_conflict_preserves_ambiguous_temp_for_recovery(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    working_one = _publish_request(
        harness,
        _working_request(
            harness,
            expected_task_version=2,
            parent_snapshot_id=baseline.snapshot_id,
            expected_pointer_version=None,
            expected_pointer_snapshot=None,
            idempotency_key="working-one",
        ),
    )
    request = _working_request(
        harness,
        expected_task_version=3,
        parent_snapshot_id=working_one.snapshot_id,
        expected_pointer_version=0,
        expected_pointer_snapshot=working_one.snapshot_id,
        idempotency_key="ambiguous-cleanup",
    )
    reservation = harness.service.reserve_operation(request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    alias = isolated_kb_home / "ambiguous-temp-alias.sqlite"
    os.link(reservation.paths.temp_path, alias)
    _introduce_prepublication_conflict(isolated_kb_home, "task")

    with pytest.raises(LifecycleOperationError) as recovery:
        harness.service.publish_artifact(reservation, descriptor)

    assert recovery.value.code == "OPERATION_RECOVERY_REQUIRED"
    assert reservation.paths.temp_path.is_file()
    assert alias.is_file()
    assert not reservation.paths.final_path.exists()
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        phase = conn.execute(
            """SELECT operation_phase FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
    assert phase == ("RECOVERY_REQUIRED",)


def test_stale_latest_working_pointer_orphans_file_without_rollback(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline_reservation = harness.service.reserve_operation(harness.request, harness.lease)
    baseline_reservation, baseline_descriptor = _capture(harness, baseline_reservation)
    baseline = harness.service.publish_artifact(baseline_reservation, baseline_descriptor)
    _activate_task(isolated_kb_home)

    working_one_request = _working_request(
        harness,
        expected_task_version=2,
        parent_snapshot_id=baseline.snapshot_id,
        expected_pointer_version=None,
        expected_pointer_snapshot=None,
        idempotency_key="working-one",
    )
    working_one_reservation = harness.service.reserve_operation(
        working_one_request,
        harness.lease,
    )
    working_one_reservation, working_one_descriptor = _capture(
        harness,
        working_one_reservation,
    )
    working_one = harness.service.publish_artifact(
        working_one_reservation,
        working_one_descriptor,
    )

    def advance_pointer(checkpoint: str) -> None:
        if checkpoint != "before_registry_finalize":
            return
        fake_snapshot_id = "f" * 32
        with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
            prior = conn.execute(
                "SELECT * FROM snapshot_generations WHERE snapshot_id = ?",
                (working_one.snapshot_id,),
            ).fetchone()
            assert prior is not None
            conn.execute(
                """INSERT INTO snapshot_generations (
                       snapshot_id, project_id, workspace_id, workspace_binding_generation,
                       task_id, generation_sequence, parent_snapshot_id, capture_purpose,
                       origin_operation_id, capture_contract_fingerprint,
                       repository_evidence_json, relative_storage_path, storage_layout_version,
                       file_size, file_sha256, truth_claim, generation_state, row_version,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 99, ?, 'TASK_WORKING', ?, ?, ?, ?, 1,
                             1, ?, 'CAPTURED_STABLE', 'AVAILABLE', 0, ?, ?)""",
                (
                    fake_snapshot_id,
                    prior[1],
                    prior[2],
                    prior[3],
                    prior[4],
                    working_one.snapshot_id,
                    baseline.operation_id,
                    prior[9],
                    prior[10],
                    f"generations/ff/{fake_snapshot_id}.sqlite",
                    "9" * 64,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            )
            conn.execute(
                """UPDATE managed_pointers
                   SET snapshot_id = ?, pointer_version = pointer_version + 1, updated_at = ?
                   WHERE pointer_id = ?""",
                (fake_snapshot_id, TIMESTAMP, WORKING_POINTER_ID),
            )

    conflict_service = LifecycleGenerationService(
        home=isolated_kb_home,
        checkpoint=advance_pointer,
    )
    working_two_request = _working_request(
        harness,
        expected_task_version=3,
        parent_snapshot_id=working_one.snapshot_id,
        expected_pointer_version=0,
        expected_pointer_snapshot=working_one.snapshot_id,
        idempotency_key="working-two",
    )
    working_two_reservation = conflict_service.reserve_operation(
        working_two_request,
        harness.lease,
    )
    harness.service = conflict_service
    working_two_reservation, working_two_descriptor = _capture(
        harness,
        working_two_reservation,
    )

    with pytest.raises(LifecycleOperationError) as conflict:
        conflict_service.publish_artifact(working_two_reservation, working_two_descriptor)

    assert conflict.value.code == "POINTER_CAS_CONFLICT"
    assert conflict.value.details["generation_state"] == "ORPHANED"
    assert working_two_reservation.paths.final_path.is_file()
    orphan = conflict_service.resolve_generation(
        working_two_reservation.snapshot_id,
        allowed_states=frozenset({"ORPHANED"}),
    )
    assert orphan.generation_state == "ORPHANED"
    with pytest.raises(LifecycleOperationError) as blocked:
        conflict_service.resolve_generation(working_two_reservation.snapshot_id)
    assert blocked.value.code == "GENERATION_STATE_BLOCKED"

    real_hash = generation_module._hash_file

    def hash_while_state_changes(path: Path) -> str:
        with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
            conn.execute(
                """UPDATE snapshot_generations
                   SET generation_state = 'QUARANTINED', row_version = row_version + 1,
                       updated_at = ?
                   WHERE snapshot_id = ?""",
                (TIMESTAMP, working_two_reservation.snapshot_id),
            )
        return real_hash(path)

    monkeypatch.setattr(generation_module, "_hash_file", hash_while_state_changes)
    with pytest.raises(LifecycleOperationError) as changed:
        conflict_service.resolve_generation(
            working_two_reservation.snapshot_id,
            allowed_states=frozenset({"ORPHANED", "QUARANTINED"}),
        )
    assert changed.value.code == "GENERATION_STATE_CHANGED"
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        pointer = conn.execute(
            "SELECT snapshot_id, pointer_version FROM managed_pointers WHERE pointer_id = ?",
            (WORKING_POINTER_ID,),
        ).fetchone()
        operation = conn.execute(
            "SELECT operation_phase FROM lifecycle_operations WHERE operation_id = ?",
            (working_two_reservation.operation_id,),
        ).fetchone()
    assert pointer == ("f" * 32, 1)
    assert operation == ("FAILED",)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "sidecar"])
def test_committed_missing_or_corrupt_file_fails_closed_without_fallback(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    damage: str,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    harness.service.publish_artifact(reservation, descriptor)
    if damage == "missing":
        reservation.paths.final_path.unlink()
    elif damage == "corrupt":
        with reservation.paths.final_path.open("ab") as stream:
            stream.write(b"corruption")
    else:
        Path(f"{reservation.paths.final_path}-wal").write_bytes(b"unexpected-sidecar")

    with pytest.raises(LifecycleOperationError) as broken:
        harness.service.recover_operation(harness.request, harness.lease)

    assert broken.value.code == "BROKEN_POINTER"
    assert broken.value.details["snapshot_id"] == reservation.snapshot_id
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        pointer = conn.execute(
            "SELECT snapshot_id FROM managed_pointers WHERE pointer_id = ?",
            (BASELINE_POINTER_ID,),
        ).fetchone()
        operation = conn.execute(
            "SELECT operation_phase FROM lifecycle_operations WHERE operation_id = ?",
            (reservation.operation_id,),
        ).fetchone()
    assert pointer == (reservation.snapshot_id,)
    assert operation == ("COMMITTED",)


def test_broken_unpointed_generation_is_quarantined_without_fallback(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    baseline = _publish_baseline(harness)
    _activate_task(isolated_kb_home)
    working_one_request = _working_request(
        harness,
        expected_task_version=2,
        parent_snapshot_id=baseline.snapshot_id,
        expected_pointer_version=None,
        expected_pointer_snapshot=None,
        idempotency_key="working-one",
    )
    working_one = _publish_request(harness, working_one_request)
    working_two = _publish_request(
        harness,
        _working_request(
            harness,
            expected_task_version=3,
            parent_snapshot_id=working_one.snapshot_id,
            expected_pointer_version=0,
            expected_pointer_snapshot=working_one.snapshot_id,
            idempotency_key="working-two",
        ),
    )
    working_one_path = harness.service.resolve_generation(working_one.snapshot_id).path
    working_one_path.unlink()

    with pytest.raises(LifecycleOperationError) as broken:
        harness.service.recover_operation(working_one_request, harness.lease)

    assert broken.value.code == "GENERATION_BROKEN"
    assert broken.value.details["generation_state"] == "QUARANTINED"
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        state = conn.execute(
            "SELECT generation_state FROM snapshot_generations WHERE snapshot_id = ?",
            (working_one.snapshot_id,),
        ).fetchone()
        pointer = conn.execute(
            """SELECT snapshot_id, pointer_version FROM managed_pointers
               WHERE pointer_id = ?""",
            (WORKING_POINTER_ID,),
        ).fetchone()
    assert state == ("QUARANTINED",)
    assert pointer == (working_two.snapshot_id, 1)


def test_hashing_and_publication_do_not_hold_registry_write_transaction(
    isolated_kb_home: Path,
    temp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_harness(isolated_kb_home, temp_git_repo)
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    real_hash = generation_module._hash_file
    observations: list[str] = []

    def unlocked_hash(path: Path) -> str:
        with sqlite3.connect(registry_path(isolated_kb_home), isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            observations.append(path.name)
            conn.rollback()
        return real_hash(path)

    monkeypatch.setattr(generation_module, "_hash_file", unlocked_hash)
    result = harness.service.publish_artifact(reservation, descriptor)

    assert observations == [
        reservation.paths.temp_path.name,
        reservation.paths.final_path.name,
    ]
    assert result.generation_state == "AVAILABLE"


def test_legacy_and_lifecycle_publication_remain_strictly_isolated(
    isolated_kb_home: Path,
    temp_git_repo: Path,
) -> None:
    (temp_git_repo / "lifecycle.py").write_text("VALUE = 7\n", encoding="utf-8")
    (temp_git_repo / "generation_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = RegistryService(home=isolated_kb_home).register("repo-one", temp_git_repo).project
    assert project is not None
    legacy = IndexService(home=isolated_kb_home)
    legacy.index("repo-one")
    with sqlite3.connect(registry_path(isolated_kb_home)) as conn:
        assert _lifecycle_counts(conn) == {
            "lifecycle_tasks": 0,
            "lifecycle_operations": 0,
            "snapshot_generations": 0,
            "managed_pointers": 0,
        }
    canonical = Path(project.storage_path) / "kb.sqlite"
    canonical_before = canonical.read_bytes()
    project_before = RegistryService(home=isolated_kb_home).find_project_by_name("repo-one")
    run_files_before = _run_file_bytes(Path(project.storage_path) / "runs")
    git_before = _git_status(temp_git_repo)
    git_storage_before = _path_manifest(temp_git_repo / ".git")
    worktree_before = _path_manifest(temp_git_repo, excluded_roots={".git"})
    harness = _setup_harness(
        isolated_kb_home,
        temp_git_repo,
        existing_project=project,
    )
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    harness.service.publish_artifact(reservation, descriptor)

    assert canonical.read_bytes() == canonical_before
    assert RegistryService(home=isolated_kb_home).find_project_by_name("repo-one") == project_before
    assert _run_file_bytes(Path(project.storage_path) / "runs") == run_files_before
    assert _git_status(temp_git_repo) == git_before
    assert _path_manifest(temp_git_repo / ".git") == git_storage_before
    assert _path_manifest(temp_git_repo, excluded_roots={".git"}) == worktree_before
    assert not hasattr(RegistryService, "compare_and_swap_pointer")
    assert not hasattr(harness.service, "project_baseline")


def _setup_harness(
    home: Path,
    repo: Path,
    *,
    checkpoint: Any = None,
    clock: Any = None,
    lease: OperationLease | None = None,
    existing_project: ProjectRecord | None = None,
) -> _Harness:
    (repo / "generation_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = existing_project or RegistryService(home=home).register("repo-one", repo).project
    assert project is not None
    contract = CaptureContract.current(ScanPolicy())
    head = _git_text(repo, "rev-parse", "HEAD")
    branch = _git_text(repo, "branch", "--show-current") or None
    with sqlite3.connect(registry_path(home)) as conn:
        conn.execute(
            """INSERT INTO lifecycle_tasks (
                   task_id, project_id, workspace_id, workspace_binding_generation,
                   task_state, capture_contract_fingerprint, baseline_head_commit,
                   baseline_head_ref, next_generation_sequence,
                   project_baseline_snapshot_id, project_baseline_pointer_version,
                   row_version, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, 0, NULL, NULL, 0, ?, ?)""",
            (
                TASK_ID,
                project.project_id,
                project.workspace_id,
                project.repo_binding_generation,
                capture_contract_fingerprint(contract),
                head,
                branch,
                TIMESTAMP,
                TIMESTAMP,
            ),
        )
    request = GenerationReservationRequest(
        idempotency_key="baseline-operation",
        operation_kind="BEGIN_TASK",
        project_id=project.project_id,
        workspace_id=project.workspace_id,
        workspace_binding_generation=project.repo_binding_generation,
        task_id=TASK_ID,
        expected_task_version=0,
        parent_snapshot_id=None,
        pointer_expectations=(
            PointerExpectation(
                pointer_id=BASELINE_POINTER_ID,
                pointer_role="TASK_BASELINE",
                expected_pointer_version=None,
                expected_snapshot_id=None,
            ),
        ),
        expected_head=head,
        expected_branch=branch,
        actor_context={"actor": "test-boundary", "invocation": "stage7b2"},
    )
    resolved_lease = lease or OperationLease(owner="builder", token="lease-one")
    service = LifecycleGenerationService(
        home=home,
        clock=clock,
        checkpoint=checkpoint,
    )
    return _Harness(
        home=home,
        repo=repo,
        project=project,
        contract=contract,
        request=request,
        lease=resolved_lease,
        service=service,
    )


def _capture(
    harness: _Harness,
    reservation: OperationReservation,
) -> tuple[OperationReservation, SealedArtifactDescriptor]:
    reservation = harness.service.begin_build(reservation)
    workspace = CaptureWorkspace(
        project_id=reservation.project_id,
        workspace_id=reservation.workspace_id,
        repo_root_norm=reservation.repository_root_norm,
        repository_identity_hash=reservation.repository_identity_hash,
        binding_generation=reservation.workspace_binding_generation,
    )
    index_service = IndexService(home=harness.home)

    def binding_matches(actual_root: Path, expected: CaptureWorkspace) -> bool:
        return index_service._active_binding_matches("repo-one", actual_root, expected)

    descriptor = capture_trusted_artifact(
        CaptureRequest(
            workspace=workspace,
            repository_root=harness.repo,
            attempts=(
                CaptureAttempt(
                    artifact_path=reservation.paths.temp_path,
                    snapshot_id=reservation.snapshot_id,
                    run_id=uuid.uuid4().hex,
                ),
            ),
            contract=harness.contract,
            workspace_matches_repository=binding_matches,
        )
    )
    return reservation, descriptor


def _activate_task(home: Path) -> None:
    with sqlite3.connect(registry_path(home)) as conn:
        conn.execute(
            """UPDATE lifecycle_tasks
               SET task_state = 'ACTIVE', row_version = row_version + 1, updated_at = ?
               WHERE task_id = ?""",
            (TIMESTAMP, TASK_ID),
        )


def _working_request(
    harness: _Harness,
    *,
    expected_task_version: int,
    parent_snapshot_id: str,
    expected_pointer_version: int | None,
    expected_pointer_snapshot: str | None,
    idempotency_key: str,
) -> GenerationReservationRequest:
    return GenerationReservationRequest(
        idempotency_key=idempotency_key,
        operation_kind="REFRESH_WORKING",
        project_id=harness.project.project_id,
        workspace_id=harness.project.workspace_id,
        workspace_binding_generation=harness.project.repo_binding_generation,
        task_id=TASK_ID,
        expected_task_version=expected_task_version,
        parent_snapshot_id=parent_snapshot_id,
        pointer_expectations=(
            PointerExpectation(
                pointer_id=WORKING_POINTER_ID,
                pointer_role="TASK_LATEST_WORKING",
                expected_pointer_version=expected_pointer_version,
                expected_snapshot_id=expected_pointer_snapshot,
            ),
        ),
        expected_head=harness.request.expected_head,
        expected_branch=harness.request.expected_branch,
        actor_context={"actor": "test-boundary", "invocation": "stage7b2"},
    )


def _final_request(
    harness: _Harness,
    *,
    expected_task_version: int,
    parent_snapshot_id: str,
    idempotency_key: str,
) -> GenerationReservationRequest:
    return GenerationReservationRequest(
        idempotency_key=idempotency_key,
        operation_kind="ACCEPT_TASK",
        project_id=harness.project.project_id,
        workspace_id=harness.project.workspace_id,
        workspace_binding_generation=harness.project.repo_binding_generation,
        task_id=TASK_ID,
        expected_task_version=expected_task_version,
        parent_snapshot_id=parent_snapshot_id,
        pointer_expectations=(
            PointerExpectation(
                pointer_id="d" * 32,
                pointer_role="TASK_FINAL",
                expected_pointer_version=None,
                expected_snapshot_id=None,
            ),
        ),
        expected_head=harness.request.expected_head,
        expected_branch=harness.request.expected_branch,
        actor_context={"actor": "test-boundary", "invocation": "stage7b2"},
    )


def _lifecycle_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "lifecycle_tasks",
            "lifecycle_operations",
            "snapshot_generations",
            "managed_pointers",
        )
    }


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_text(repo: Path, *args: str) -> str:
    result = git(repo, *args, text=True)
    assert isinstance(result.stdout, str)
    return result.stdout.strip()


def _git_status(repo: Path) -> bytes:
    result = git(repo, "status", "--porcelain=v2", "--branch", "--untracked-files=all")
    assert isinstance(result.stdout, bytes)
    return result.stdout


def _run_file_bytes(path: Path) -> dict[str, bytes]:
    return {candidate.name: candidate.read_bytes() for candidate in path.iterdir()}


def _path_manifest(
    root: Path,
    *,
    excluded_roots: set[str] | None = None,
) -> dict[str, tuple[int, str]]:
    excluded = excluded_roots or set()
    return {
        candidate.relative_to(root).as_posix(): (
            candidate.stat().st_size,
            _sha256(candidate),
        )
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.relative_to(root).parts[0] not in excluded
    }


def _publish_baseline(harness: _Harness) -> Any:
    return _publish_request(harness, harness.request)


def _publish_request(harness: _Harness, request: GenerationReservationRequest) -> Any:
    reservation = harness.service.reserve_operation(request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    return harness.service.publish_artifact(reservation, descriptor)


def _prepare_locked_matching_duplicate(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OperationReservation, bytes, dict[str, bool]]:
    reservation = harness.service.reserve_operation(harness.request, harness.lease)
    reservation, descriptor = _capture(harness, reservation)
    exact_bytes = reservation.paths.temp_path.read_bytes()
    reservation.paths.final_path.write_bytes(exact_bytes)
    temp_locked = {"active": True}
    real_unlink = Path.unlink

    def sharing_violation(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == reservation.paths.temp_path and temp_locked["active"]:
            raise PermissionError(13, "injected Windows sharing violation", str(path))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", sharing_violation)
    with pytest.raises(LifecycleOperationError) as blocked:
        harness.service.publish_artifact(reservation, descriptor)
    assert blocked.value.code == "PUBLICATION_SHARING_VIOLATION"
    assert blocked.value.retryable
    assert reservation.paths.final_path.read_bytes() == exact_bytes
    assert reservation.paths.temp_path.read_bytes() == exact_bytes
    with sqlite3.connect(registry_path(harness.home)) as conn:
        operation = conn.execute(
            """SELECT operation_phase, failure_json FROM lifecycle_operations
               WHERE operation_id = ?""",
            (reservation.operation_id,),
        ).fetchone()
        assert _lifecycle_counts(conn)["snapshot_generations"] == 0
        assert _lifecycle_counts(conn)["managed_pointers"] == 0
    assert operation is not None and operation[0] == "RECOVERY_REQUIRED"
    assert json.loads(operation[1])["code"] == "DUPLICATE_TEMP_CLEANUP_BLOCKED"
    return reservation, exact_bytes, temp_locked


def _tamper_reservation(
    reservation: OperationReservation,
    field: str,
) -> OperationReservation:
    if field == "home":
        return replace(
            reservation,
            paths=replace(reservation.paths, home=reservation.paths.home / "forged"),
        )
    if field == "temp_path":
        return replace(
            reservation,
            paths=replace(
                reservation.paths,
                temp_path=reservation.paths.temp_path.with_name("forged.tmp.sqlite"),
            ),
        )
    if field == "final_path":
        return replace(
            reservation,
            paths=replace(
                reservation.paths,
                final_path=reservation.paths.final_path.with_name("forged.sqlite"),
            ),
        )
    if field == "pointer_id":
        pointer = reservation.pointer_expectations[0]
        return replace(
            reservation,
            pointer_expectations=(replace(pointer, pointer_id="f" * 32),),
        )
    if field == "pointer_version":
        pointer = reservation.pointer_expectations[0]
        return replace(
            reservation,
            pointer_expectations=(replace(pointer, expected_pointer_version=99),),
        )
    if field == "pointer_predecessor":
        pointer = reservation.pointer_expectations[0]
        return replace(
            reservation,
            pointer_expectations=(replace(pointer, expected_snapshot_id="f" * 32),),
        )
    if field == "parent":
        return replace(reservation, parent_snapshot_id="f" * 32)
    if field == "binding":
        return replace(reservation, workspace_binding_generation="f" * 32)
    if field == "operation_kind":
        return replace(reservation, operation_kind="REFRESH_WORKING")
    if field == "operation_phase":
        return replace(
            reservation,
            operation_phase=(
                "BUILDING" if reservation.operation_phase == "RESERVED" else "RESERVED"
            ),
        )
    if field == "expected_head":
        return replace(reservation, expected_head="f" * 40)
    if field == "expected_branch":
        return replace(reservation, expected_branch="forged-branch")
    if field == "capture_contract":
        return replace(reservation, capture_contract_fingerprint="f" * 64)
    if field == "repository_root":
        return replace(reservation, repository_root_norm="C:/forged/repository")
    if field == "repository_identity":
        return replace(reservation, repository_identity_hash="f" * 64)
    if field == "lease_token":
        return replace(reservation, lease_token="forged-lease-token")
    raise AssertionError(f"unknown reservation field: {field}")


def _registry_state(home: Path) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(registry_path(home)) as conn:
        return {
            "tasks": conn.execute(
                """SELECT task_id, task_state, next_generation_sequence, row_version
                   FROM lifecycle_tasks ORDER BY task_id"""
            ).fetchall(),
            "operations": conn.execute(
                """SELECT operation_id, operation_phase, failure_json, row_version
                   FROM lifecycle_operations ORDER BY operation_id"""
            ).fetchall(),
            "generations": conn.execute(
                """SELECT snapshot_id, generation_sequence, parent_snapshot_id,
                          capture_purpose, generation_state, row_version
                   FROM snapshot_generations ORDER BY snapshot_id"""
            ).fetchall(),
            "pointers": conn.execute(
                """SELECT pointer_id, pointer_role, snapshot_id, pointer_version
                   FROM managed_pointers ORDER BY pointer_id"""
            ).fetchall(),
            "events": conn.execute(
                """SELECT event_type, details_json FROM registry_events
                   WHERE event_type LIKE 'lifecycle_generation_%'
                   ORDER BY created_at, event_id"""
            ).fetchall(),
        }


def _insert_generation_from_row(
    conn: sqlite3.Connection,
    prior: sqlite3.Row | tuple[Any, ...],
    *,
    snapshot_id: str,
    generation_sequence: int,
    parent_snapshot_id: str,
    relative_path: str,
) -> None:
    conn.execute(
        """INSERT INTO snapshot_generations (
               snapshot_id, project_id, workspace_id, workspace_binding_generation,
               task_id, generation_sequence, parent_snapshot_id, capture_purpose,
               origin_operation_id, capture_contract_fingerprint,
               repository_evidence_json, relative_storage_path, storage_layout_version,
               file_size, file_sha256, truth_claim, generation_state, row_version,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'TASK_WORKING', ?, ?, ?, ?, 1,
                     1, ?, 'CAPTURED_STABLE', 'AVAILABLE', 0, ?, ?)""",
        (
            snapshot_id,
            prior[1],
            prior[2],
            prior[3],
            prior[4],
            generation_sequence,
            parent_snapshot_id,
            prior[8],
            prior[9],
            prior[10],
            relative_path,
            "9" * 64,
            TIMESTAMP,
            TIMESTAMP,
        ),
    )


def _introduce_prepublication_conflict(home: Path, conflict_kind: str) -> None:
    with sqlite3.connect(registry_path(home)) as conn:
        if conflict_kind == "pointer":
            trigger_sql = conn.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type = 'trigger' AND name = 'managed_pointers_update_guard'"""
            ).fetchone()[0]
            conn.execute("DROP TRIGGER managed_pointers_update_guard")
            conn.execute(
                """UPDATE managed_pointers
                   SET pointer_version = pointer_version + 1, updated_at = ?
                   WHERE pointer_id = ?""",
                (TIMESTAMP, WORKING_POINTER_ID),
            )
            conn.execute(trigger_sql)
        elif conflict_kind == "task":
            conn.execute(
                """UPDATE lifecycle_tasks
                   SET row_version = row_version + 1, updated_at = ?
                   WHERE task_id = ?""",
                (TIMESTAMP, TASK_ID),
            )
        elif conflict_kind == "workspace":
            conn.execute(
                """UPDATE workspaces
                   SET workspace_state = 'UNAVAILABLE', row_version = row_version + 1,
                       updated_at = ?
                   WHERE workspace_id = (
                       SELECT workspace_id FROM lifecycle_tasks WHERE task_id = ?
                   )""",
                (TIMESTAMP, TASK_ID),
            )
        else:
            raise AssertionError(f"unknown conflict kind: {conflict_kind}")


def _restore_workspace_active(home: Path) -> None:
    with sqlite3.connect(registry_path(home)) as conn:
        conn.execute(
            """UPDATE workspaces
               SET workspace_state = 'ACTIVE', row_version = row_version + 1, updated_at = ?
               WHERE workspace_id = (
                   SELECT workspace_id FROM lifecycle_tasks WHERE task_id = ?
               )""",
            (TIMESTAMP, TASK_ID),
        )


def _rewrite_operation_plan(
    conn: sqlite3.Connection,
    operation_id: str,
    plan: dict[str, Any],
) -> None:
    trigger_sql = conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type = 'trigger' AND name = 'lifecycle_operations_update_guard'"""
    ).fetchone()[0]
    conn.execute("DROP TRIGGER lifecycle_operations_update_guard")
    conn.execute(
        """UPDATE lifecycle_operations SET expected_pointer_versions_json = ?
           WHERE operation_id = ?""",
        (json.dumps(plan, sort_keys=True, separators=(",", ":")), operation_id),
    )
    conn.execute(trigger_sql)
