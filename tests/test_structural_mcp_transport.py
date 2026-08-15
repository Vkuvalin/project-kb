import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from _git_support import git
from mcp.types import CallToolResult

from project_kb.indexing.service import IndexService
from project_kb.registry import RegistryService
from project_kb.snapshot import SnapshotReader
from project_kb.transport.mcp_server import create_server
from project_kb.transport.structural import (
    MAX_PATH_LENGTH,
    MAX_PATHS,
    FrozenSnapshotConfig,
    QueryBoundaryError,
    StructuralTransportService,
)


@pytest.fixture
def structural_service(temp_git_repo: Path) -> StructuralTransportService:
    package = temp_git_repo / "src" / "example_project"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    lines = ["class PhotoRepository:\n", "    pass\n", "\n"]
    lines.extend(
        f"def symbol_{index}(value: int) -> int:\n    return value\n\n" for index in range(40)
    )
    (package / "repositories.py").write_text("".join(lines), encoding="utf-8")
    (package / "consumer.py").write_text(
        "from example_project.repositories import PhotoRepository, symbol_1\n",
        encoding="utf-8",
    )
    git(temp_git_repo, "add", "src", text=True)
    git(temp_git_repo, "commit", "-m", "Add structural fixture", text=True)

    project = RegistryService().register("repo-one", temp_git_repo).project
    assert project is not None
    IndexService().index("repo-one")
    snapshot_path = Path(project.storage_path) / "kb.sqlite"
    reader = SnapshotReader(snapshot_path, project_id=project.project_id, active_binding=None)
    return StructuralTransportService(
        FrozenSnapshotConfig(
            path=snapshot_path,
            project_id=project.project_id,
            snapshot_id=reader.meta["snapshot_id"],
            artifact_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            source_commit=reader.meta["git_head"],
        )
    )


def _payload(raw: str) -> dict:
    payload = json.loads(raw)
    assert payload["response_bytes"] == len(raw.encode("utf-8"))
    assert payload["telemetry"]["response_bytes"] == payload["response_bytes"]
    return payload


def _canonical_payload_bytes(payload: dict) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def test_metadata_and_multiple_calls_share_one_process_and_bootstrap(
    structural_service: StructuralTransportService,
) -> None:
    first = _payload(structural_service.get_snapshot_metadata())
    second = _payload(
        structural_service.find_symbols(
            names=["PhotoRepository", "symbol_1"],
            projection=["relative_path", "short_name", "symbol_kind"],
        )
    )

    assert first["source_ref"] == second["source_ref"]
    assert first["telemetry"]["process_id"] == second["telemetry"]["process_id"]
    assert first["telemetry"]["server_id"] == second["telemetry"]["server_id"]
    assert first["telemetry"]["process_start_count"] == 1
    assert second["telemetry"]["process_start_count"] == 1
    assert second["telemetry"]["call_id"] == first["telemetry"]["call_id"] + 1
    assert second["telemetry"]["bootstrap_count"] == 1
    assert second["telemetry"]["snapshot_validation_count"] == 1
    assert {record["short_name"] for record in second["records"]} == {
        "PhotoRepository",
        "symbol_1",
    }


def test_pagination_is_deterministic_and_bound_to_source_and_query(
    structural_service: StructuralTransportService,
) -> None:
    first = _payload(
        structural_service.query_structural_records(
            dataset="symbols",
            operation="rows",
            projection=["relative_path", "short_name", "start_line"],
            record_limit=7,
        )
    )
    assert first["returned"] == 7
    assert first["truncated"] is True
    assert first["next_cursor"]

    second = _payload(
        structural_service.query_structural_records(
            dataset="symbols",
            operation="rows",
            projection=["relative_path", "short_name", "start_line"],
            record_limit=7,
            cursor=first["next_cursor"],
        )
    )
    assert first["records"] != second["records"]

    with pytest.raises(QueryBoundaryError, match="does not belong"):
        structural_service.query_structural_records(
            dataset="symbols",
            operation="rows",
            projection=["short_name"],
            record_limit=7,
            cursor=first["next_cursor"],
        )


def test_hard_response_byte_limit_truncates_at_a_complete_record(
    structural_service: StructuralTransportService,
) -> None:
    payload = _payload(
        structural_service.query_structural_records(
            dataset="symbols",
            operation="rows",
            projection=["relative_path", "qualified_name", "signature_text"],
            record_limit=100,
            response_byte_limit=2_400,
        )
    )

    assert payload["ok"] is True
    assert payload["response_bytes"] <= 2_400
    assert 0 < payload["returned"] < payload["total"]
    assert payload["truncated"] is True
    assert payload["next_cursor"]


def test_generic_count_group_count_and_batch_lookup_are_allowlisted(
    structural_service: StructuralTransportService,
) -> None:
    count = _payload(
        structural_service.query_structural_records(
            dataset="symbols",
            operation="count",
            filters=[{"field": "symbol_kind", "op": "eq", "value": "FUNCTION"}],
        )
    )
    grouped = _payload(
        structural_service.query_structural_records(
            dataset="symbols",
            operation="group_count",
            group_by=["symbol_kind", "binding_role"],
        )
    )
    lookup = _payload(
        structural_service.query_structural_records(
            dataset="symbols",
            operation="batch_lookup",
            lookup_field="short_name",
            lookup_values=["PhotoRepository", "symbol_1"],
            projection=["short_name", "symbol_kind"],
        )
    )

    assert count["records"][0]["count"] == 40
    assert sum(record["count"] for record in grouped["records"]) > 40
    assert {record["short_name"] for record in lookup["records"]} == {
        "PhotoRepository",
        "symbol_1",
    }

    with pytest.raises(QueryBoundaryError, match="Dataset is not allowlisted"):
        structural_service.query_structural_records(dataset="sqlite_master")  # type: ignore[arg-type]


def test_inspect_paths_is_batched_bounded_and_rejects_absolute_paths(
    structural_service: StructuralTransportService,
) -> None:
    payload = _payload(
        structural_service.inspect_paths(
            paths=[
                "src/example_project/repositories.py",
                "src/example_project/consumer.py",
            ],
            include=["file", "symbols", "imports", "diagnostics"],
            per_path_limit=10,
        )
    )

    assert payload["returned"] == 2
    assert all(item["found"] for item in payload["paths"])
    assert all(item["returned"] <= 31 for item in payload["paths"])
    assert any(item["truncated"] for item in payload["paths"])

    with pytest.raises(QueryBoundaryError, match="repository-relative"):
        structural_service.inspect_paths(paths=[str(structural_service.config.path)])


@pytest.mark.parametrize("tool", ["find_symbols", "inspect_paths"])
def test_path_lists_accept_the_hard_maximum_and_reject_one_above_it(
    structural_service: StructuralTransportService,
    tool: str,
) -> None:
    maximum_paths = [f"src/example_project/path_{index:02d}.py" for index in range(MAX_PATHS)]

    if tool == "find_symbols":
        raw = structural_service.find_symbols(names=["PhotoRepository"], paths=maximum_paths)
        call = lambda paths: structural_service.find_symbols(  # noqa: E731
            names=["PhotoRepository"], paths=paths
        )
    else:
        raw = structural_service.inspect_paths(paths=maximum_paths, include=["file"])
        call = lambda paths: structural_service.inspect_paths(paths=paths)  # noqa: E731

    assert _payload(raw)["ok"] is True
    with pytest.raises(QueryBoundaryError) as caught:
        call([*maximum_paths, "src/example_project/one_too_many.py"])
    assert caught.value.code == "QUERY_INPUT_INVALID"
    assert str(caught.value) == f"paths must contain at most {MAX_PATHS} path values."


@pytest.mark.parametrize("tool", ["find_symbols", "inspect_paths"])
def test_path_values_accept_the_hard_maximum_length_and_reject_one_above_it(
    structural_service: StructuralTransportService,
    tool: str,
) -> None:
    maximum_path = "src/" + ("a" * (MAX_PATH_LENGTH - len("src/")))
    oversized_path = maximum_path + "a"

    if tool == "find_symbols":
        raw = structural_service.find_symbols(names=["PhotoRepository"], paths=[maximum_path])
        call = lambda path: structural_service.find_symbols(  # noqa: E731
            names=["PhotoRepository"], paths=[path]
        )
    else:
        raw = structural_service.inspect_paths(paths=[maximum_path], include=["file"])
        call = lambda path: structural_service.inspect_paths(  # noqa: E731
            paths=[path], include=["file"]
        )

    assert _payload(raw)["ok"] is True
    with pytest.raises(QueryBoundaryError) as caught:
        call(oversized_path)
    assert caught.value.code == "QUERY_INPUT_INVALID"
    assert str(caught.value) == f"Path must not exceed {MAX_PATH_LENGTH} characters."


def test_mcp_server_exposes_only_the_four_approved_structural_tools(
    structural_service: StructuralTransportService,
) -> None:
    server = create_server(structural_service)

    assert set(server._tool_manager._tools) == {  # noqa: SLF001 - focused surface contract
        "get_snapshot_metadata",
        "find_symbols",
        "query_structural_records",
        "inspect_paths",
    }


def test_all_mcp_tools_publish_and_return_structured_results_without_text_payloads(
    structural_service: StructuralTransportService,
) -> None:
    server = create_server(structural_service)
    calls = {
        "get_snapshot_metadata": {},
        "find_symbols": {"names": ["PhotoRepository"]},
        "inspect_paths": {
            "paths": ["src/example_project/repositories.py"],
            "include": ["file"],
        },
        "query_structural_records": {
            "dataset": "symbols",
            "operation": "count",
        },
    }

    async def invoke_all() -> dict[str, CallToolResult]:
        return {
            name: await server._tool_manager.call_tool(  # noqa: SLF001 - SDK boundary contract
                name,
                arguments,
                context=None,  # type: ignore[arg-type]
                convert_result=True,
            )
            for name, arguments in calls.items()
        }

    results = asyncio.run(invoke_all())
    for name, result in results.items():
        registered = server._tool_manager._tools[name]  # noqa: SLF001 - SDK schema contract
        assert registered.output_schema is not None
        assert registered.output_schema["type"] == "object"
        assert "response_bytes" in registered.output_schema["properties"]
        assert isinstance(result, CallToolResult)
        assert result.content == []
        assert isinstance(result.structured_content, dict)
        payload = result.structured_content
        assert payload["tool"] == name
        assert payload["response_bytes"] == _canonical_payload_bytes(payload)
        assert payload["telemetry"]["response_bytes"] == payload["response_bytes"]

    find_paths_schema = server._tool_manager._tools[  # noqa: SLF001 - SDK input contract
        "find_symbols"
    ].parameters["properties"]["paths"]
    optional_path_array = next(
        option for option in find_paths_schema["anyOf"] if option.get("type") == "array"
    )
    inspect_path_array = server._tool_manager._tools[  # noqa: SLF001 - SDK input contract
        "inspect_paths"
    ].parameters["properties"]["paths"]
    for schema in (optional_path_array, inspect_path_array):
        assert schema["maxItems"] == MAX_PATHS
        assert schema["items"]["maxLength"] == MAX_PATH_LENGTH
