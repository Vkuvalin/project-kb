"""STDIO MCP adapter for one operator-pinned Project KB structural snapshot."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from project_kb.transport.structural import (
    DEFAULT_RESPONSE_BYTE_LIMIT,
    MAX_PATH_LENGTH,
    MAX_PATHS,
    FrozenSnapshotConfig,
    StructuralTransportService,
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


class StructuralFilter(BaseModel):
    field: str
    op: Literal["eq", "prefix", "in", "is_null"] = "eq"
    value: str | int | bool | list[str | int | bool] | None = None


class StructuralSourceRef(BaseModel):
    project_id: str
    snapshot_id: str
    source_commit: str
    schema_version: int
    artifact_sha256: str


class StructuralTelemetry(BaseModel):
    call_id: int
    operation: str
    server_id: str
    process_id: int
    process_start_count: int
    bootstrap_count: int
    snapshot_validation_count: int
    server_started_monotonic_ns: int
    call_started_monotonic_ns: int
    call_ended_monotonic_ns: int
    latency_ms: float
    request_bytes: int
    response_bytes: int
    records_returned: int
    pages: int
    cursor_used: bool
    truncated: bool
    error: bool
    retry_count: int


class StructuralError(BaseModel):
    code: str
    message: str


class StructuralResult(BaseModel):
    """Typed MCP output envelope for all four structural capabilities."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    tool: Literal[
        "get_snapshot_metadata",
        "find_symbols",
        "query_structural_records",
        "inspect_paths",
    ]
    returned: int = Field(ge=0)
    total: int = Field(ge=0)
    truncated: bool
    next_cursor: str | None
    response_bytes: int = Field(ge=0)
    source_ref: StructuralSourceRef
    telemetry: StructuralTelemetry
    metadata: dict[str, Any] | None = None
    records: list[dict[str, Any]] | None = None
    paths: list[dict[str, Any]] | None = None
    query: dict[str, Any] | None = None
    dataset: str | None = None
    operation: str | None = None
    error: StructuralError | None = None


BoundedPath = Annotated[str, Field(min_length=1, max_length=MAX_PATH_LENGTH)]
OptionalPathList = Annotated[list[BoundedPath], Field(max_length=MAX_PATHS)]
RequiredPathList = Annotated[list[BoundedPath], Field(min_length=1, max_length=MAX_PATHS)]
StructuredToolResult = Annotated[CallToolResult, StructuralResult]


def _structured_result(raw: str) -> CallToolResult:
    payload = json.loads(raw)
    StructuralResult.model_validate(payload)
    return CallToolResult(content=[], structuredContent=payload)


def create_server(service: StructuralTransportService) -> MCPServer:
    server = MCPServer(
        "project-kb-structural",
        instructions=(
            "Read-only structural access to one frozen Project KB snapshot. "
            "Use only allowlisted filters/projections and repository-relative paths."
        ),
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_snapshot_metadata(
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> StructuredToolResult:
        """Return compact identity and provenance for the pinned frozen snapshot."""
        return _structured_result(
            service.get_snapshot_metadata(response_byte_limit=response_byte_limit)
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def find_symbols(
        names: Annotated[list[str], Field(min_length=1, max_length=50)],
        match: Literal["exact", "prefix"] = "exact",
        projection: list[str] | None = None,
        symbol_kinds: list[str] | None = None,
        paths: OptionalPathList | None = None,
        record_limit: int = 100,
        cursor: str | None = None,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> StructuredToolResult:
        """Find one or more symbols with bounded projection and deterministic paging."""
        return _structured_result(
            service.find_symbols(
                names=names,
                match=match,
                projection=projection,
                symbol_kinds=symbol_kinds,
                paths=paths,
                record_limit=record_limit,
                cursor=cursor,
                response_byte_limit=response_byte_limit,
            )
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def inspect_paths(
        paths: RequiredPathList,
        include: list[Literal["file", "symbols", "imports", "diagnostics"]] | None = None,
        per_path_limit: int = 100,
        cursor: str | None = None,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> StructuredToolResult:
        """Inspect multiple safe repository-relative paths in one bounded call."""
        return _structured_result(
            service.inspect_paths(
                paths=paths,
                include=include,
                per_path_limit=per_path_limit,
                cursor=cursor,
                response_byte_limit=response_byte_limit,
            )
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def query_structural_records(
        dataset: Literal["files", "symbols", "imports", "relations", "parse_diagnostics"],
        operation: Literal["rows", "count", "distinct", "group_count", "batch_lookup"] = "rows",
        filters: list[StructuralFilter] | None = None,
        projection: list[str] | None = None,
        distinct_field: str | None = None,
        group_by: list[str] | None = None,
        lookup_field: str | None = None,
        lookup_values: list[str | int | bool] | None = None,
        record_limit: int = 100,
        cursor: str | None = None,
        response_byte_limit: int = DEFAULT_RESPONSE_BYTE_LIMIT,
    ) -> StructuredToolResult:
        """Run a generic allowlisted structural rows/count/distinct/group/batch query."""
        normalized_filters: list[dict[str, Any]] | None = (
            [item.model_dump() for item in filters] if filters is not None else None
        )
        return _structured_result(
            service.query_structural_records(
                dataset=dataset,
                operation=operation,
                filters=normalized_filters,
                projection=projection,
                distinct_field=distinct_field,
                group_by=group_by,
                lookup_field=lookup_field,
                lookup_values=lookup_values,
                record_limit=record_limit,
                cursor=cursor,
                response_byte_limit=response_byte_limit,
            )
        )

    return server


def main() -> None:
    service = StructuralTransportService(FrozenSnapshotConfig.from_environment())
    create_server(service).run("stdio")


if __name__ == "__main__":
    main()
