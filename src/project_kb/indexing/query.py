"""Resolver-gated structural snapshot query service."""

from pathlib import Path
from typing import Any

from project_kb.errors import ProjectStatusError
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.state import ProjectState
from project_kb.snapshot.database import SnapshotReader


class QueryService:
    def __init__(self, *, home: Path | None = None, working_directory: Path | None = None) -> None:
        self.status_service = ProjectStatusService(home=home, working_directory=working_directory)

    def symbols(
        self, project_name: str | None, *, file: str | None, name: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        status, reader = self._reader(project_name)
        return self._context(status, reader), reader.symbols(file=file, name=name)

    def imports(
        self, project_name: str | None, *, file: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        status, reader = self._reader(project_name)
        return self._context(status, reader), reader.imports(file=file)

    def inspect(
        self, project_name: str | None, *, file: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        status, reader = self._reader(project_name)
        return self._context(status, reader), reader.inspect_file(file)

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
        return status, SnapshotReader(
            Path(status.project.storage_path) / "kb.sqlite", project_id=status.project.project_id
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
            "resolution": status.resolution.to_dict(),
            "project": status.project.to_dict(),
            "project_state": status.project_state.value,
            "snapshot_availability": "available",
            "last_index_outcome": status.project.last_status,
            "snapshot": {
                "snapshot_id": reader.meta["snapshot_id"],
                "created_at": reader.meta["created_at"],
                "schema_version": reader.meta["schema_version"],
                "scanner_version": reader.meta["scanner_version"],
                "policy_version": reader.meta["policy_version"],
                "extractor_versions": reader.meta["extractor_versions"],
                "compatibility": reader.meta["compatibility"],
                "currentness": "unverified",
            },
            "query_warnings": warnings,
        }
