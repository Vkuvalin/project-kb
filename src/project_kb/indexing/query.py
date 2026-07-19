"""Resolver-gated structural snapshot query service."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from project_kb.errors import ProjectStatusError, SnapshotQueryError
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.repo_identity import repository_identity_hash
from project_kb.resolver.state import ProjectState
from project_kb.snapshot.database import SnapshotReader


class SnapshotQueryAdapter:
    """One query adapter shared by canonical and exact lifecycle read targets."""

    def __init__(self, reader: SnapshotReader, context: Mapping[str, Any]) -> None:
        self.reader = reader
        self.context = dict(context)

    def symbols(
        self,
        *,
        file: str | None,
        name: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return dict(self.context), self.reader.symbols(file=file, name=name)

    def imports(
        self,
        *,
        file: str | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return dict(self.context), self.reader.imports(file=file)

    def inspect(
        self,
        *,
        file: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return dict(self.context), self.reader.inspect_file(file)


class QueryService:
    def __init__(self, *, home: Path | None = None, working_directory: Path | None = None) -> None:
        self.status_service = ProjectStatusService(home=home, working_directory=working_directory)

    def symbols(
        self, project_name: str | None, *, file: str | None, name: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self._adapter(project_name).symbols(file=file, name=name)

    def imports(
        self, project_name: str | None, *, file: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self._adapter(project_name).imports(file=file)

    def inspect(
        self, project_name: str | None, *, file: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._adapter(project_name).inspect(file=file)

    def _adapter(self, project_name: str | None) -> SnapshotQueryAdapter:
        status, reader = self._reader(project_name)
        return SnapshotQueryAdapter(reader, self._context(status, reader))

    def _reader(self, project_name: str | None) -> tuple[Any, SnapshotReader]:
        status = self.status_service.status(project_name)
        if status.project is None or not status.availability.can_use_snapshot:
            if status.error:
                raise status.error
            raise ProjectStatusError(
                code=status.code,
                message=status.message,
                exit_code=status.exit_code,
                recommended_action=status.recommended_action.code,
                details={"project_state": status.project_state.value},
            )
        expected_project = status.project

        def active_binding() -> tuple[str, str, str]:
            current = self.status_service.registry.find_project_by_name(
                expected_project.project_name
            )
            if current is None or current.project_id != expected_project.project_id:
                raise SnapshotQueryError(
                    "Project registration changed during snapshot access.",
                    code="SNAPSHOT_REBUILD_REQUIRED",
                    details={
                        "snapshot_classification": "wrong_repository_binding",
                        "reason": "registration_changed_during_query",
                    },
                )
            if current.repo_binding_generation != expected_project.repo_binding_generation:
                raise SnapshotQueryError(
                    "Project repository binding changed during snapshot access.",
                    code="SNAPSHOT_REBUILD_REQUIRED",
                    details={
                        "snapshot_classification": "wrong_repository_binding",
                        "reason": "registration_generation_changed_during_query",
                    },
                )
            return (
                current.repo_root_norm,
                repository_identity_hash(
                    current.repo_root_norm,
                    current.repo_fingerprint_json,
                ),
                current.repo_binding_generation,
            )

        return status, SnapshotReader(
            Path(status.project.storage_path) / "kb.sqlite",
            project_id=status.project.project_id,
            expected_repo_root_norm=status.project.repo_root_norm,
            expected_repository_identity_hash=repository_identity_hash(
                status.project.repo_root_norm,
                status.project.repo_fingerprint_json,
            ),
            expected_repository_binding_generation=(status.project.repo_binding_generation),
            active_binding=active_binding,
        )

    @staticmethod
    def _context(status: Any, reader: SnapshotReader) -> dict[str, Any]:
        warnings: list[dict[str, Any]] = []
        if status.project_state is ProjectState.LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE:
            warnings.append(
                {
                    "code": "USING_PREVIOUS_SNAPSHOT_AFTER_FAILED_REFRESH",
                    "message": (
                        "The query used the preserved snapshot after the latest index failed."
                    ),
                }
            )
        elif status.project_state is ProjectState.SNAPSHOT_PRESENT_REGISTRY_WARNING:
            warnings.append(
                {
                    "code": "PUBLISHED_SNAPSHOT_REGISTRY_OUTCOME_STALE",
                    "message": "The published snapshot is newer than registry outcome metadata.",
                }
            )
        return {
            "snapshot_source": "LEGACY_CANONICAL",
            "resolution": status.resolution.to_dict(),
            "project": status.project.to_dict(),
            "project_state": status.project_state.value,
            "snapshot_availability": "available",
            "last_index_outcome": status.project.last_status,
            "snapshot": {
                "snapshot_id": reader.meta["snapshot_id"],
                "snapshot_source": "LEGACY_CANONICAL",
                "created_at": reader.meta["created_at"],
                "schema_version": reader.meta["schema_version"],
                "scanner_version": reader.meta["scanner_version"],
                "policy_version": reader.meta["policy_version"],
                "extractor_versions": reader.meta["extractor_versions"],
                "compatibility": reader.meta["compatibility"],
                "availability": status.snapshot_check.availability,
                "currentness": status.snapshot_check.currentness,
                "truth_claim": status.snapshot_check.truth_claim,
                "verification_mode": status.snapshot_check.verification_mode,
            },
            "query_warnings": warnings,
        }
