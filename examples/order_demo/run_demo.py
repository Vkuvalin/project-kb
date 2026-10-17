"""Настоящие CLI и stdio MCP над отдельной временной копией shop."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ALIAS = "order-demo"
CLI = [sys.executable, "-c", "from project_kb.cli.app import main; main()"]
SYMBOL_REQUEST = {
    "names": ["Order", "OrderService"],
    "match": "exact",
    "symbol_kinds": ["CLASS"],
    "projection": ["relative_path", "short_name", "symbol_kind", "start_line", "end_line"],
}
REQUESTS = {
    "get_snapshot_metadata": {},
    "find_symbols": SYMBOL_REQUEST,
    "query_structural_records": {
        "dataset": "files",
        "projection": ["relative_path", "line_count", "parse_status"],
    },
    "inspect_paths": {
        "paths": ["shop/models.py", "shop/services.py"],
        "include": ["file", "symbols", "imports", "diagnostics"],
    },
}


def run_command(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"Command failed: {args[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def cli(args: list[str], repo: Path, env: dict[str, str], raw: dict) -> dict:
    payload = json.loads(run_command([*CLI, *args, "--json"], repo, env))
    raw["cli"].append({"arguments": list(args), "response": payload})
    if not payload["ok"]:
        raise RuntimeError(f"CLI returned {payload['code']}")
    return payload


def select(record: dict, *keys: str) -> dict:
    return {key: record[key] for key in keys}


def inspect_summary(inspection: dict) -> dict:
    return {
        "file": select(inspection["file"], "relative_path", "line_count", "parse_status"),
        "symbols": [
            select(item, "qualified_name", "symbol_kind", "start_line", "end_line")
            for item in inspection["symbols"]
        ],
        "imports": [
            select(item, "module_text", "imported_name", "resolution_status",
                   "resolved_relative_path")
            for item in inspection["imports"]
        ],
        "diagnostics": len(inspection["parse_diagnostics"]),
    }


async def mcp_demo(
    repo: Path, env: dict[str, str], raw: dict, before: dict, work: Path,
) -> dict:
    project = before["data"]["project"]
    snapshot = before["data"]["snapshot_check"]
    path = Path(project["storage_path"]) / "kb.sqlite"
    server_env = {
        **env,
        "PKB_MCP_SNAPSHOT_PATH": str(path),
        "PKB_MCP_PROJECT_ID": project["project_id"],
        "PKB_MCP_SNAPSHOT_ID": snapshot["snapshot_id"],
        "PKB_MCP_SOURCE_COMMIT": snapshot["git_commit_at_index"],
        "PKB_MCP_SNAPSHOT_SHA256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "project_kb.transport.mcp_server"],
        cwd=repo,
        env=server_env,
    )
    results = {}
    with (work / "mcp-server.log").open("w", encoding="utf-8") as error_log:
        async with stdio_client(params, errlog=error_log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                available = {tool.name for tool in tools.tools}
                if available != set(REQUESTS):
                    raise RuntimeError(f"Unexpected MCP tools: {sorted(available)}")
                for tool, arguments in REQUESTS.items():
                    response = await session.call_tool(tool, arguments)
                    payload = response.structured_content
                    if response.is_error or not payload or not payload["ok"]:
                        raise RuntimeError(f"MCP tool failed: {tool}")
                    raw["mcp"][tool] = payload
                    results[tool] = payload

                # Изменяем только временную копию, не обновляя её снимок.
                model = repo / "shop" / "models.py"
                with model.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write("\n\nclass ExpressOrder(Order):\n    pass\n")
                stale = cli(["status", ALIAS, "--verify", "strong"], repo, env, raw)
                after_request = {
                    **SYMBOL_REQUEST, "names": ["Order", "OrderService", "ExpressOrder"],
                }
                response = await session.call_tool("find_symbols", after_request)
                after = response.structured_content
                if response.is_error or not after or not after["ok"]:
                    raise RuntimeError("Reading the pinned snapshot after change failed")
                raw["mcp"]["find_symbols_after_change"] = after

    if before["data"]["snapshot_check"]["currentness"] != "CURRENT":
        raise RuntimeError("Initial strong status is not CURRENT")
    if stale["data"]["snapshot_check"]["currentness"] != "STALE":
        raise RuntimeError("Changing the fixture did not produce STALE")
    same_snapshot = after["source_ref"] == results["find_symbols"]["source_ref"]
    old_records = after["records"] == results["find_symbols"]["records"]
    if not same_snapshot or not old_records:
        raise RuntimeError("The pinned snapshot changed unexpectedly")
    if {item["short_name"] for item in after["records"]} != {"Order", "OrderService"}:
        raise RuntimeError("The old snapshot has unexpected classes")

    return {
        "tools": sorted(available),
        "metadata": select(
            results["get_snapshot_metadata"]["metadata"], "schema_version", "build_status",
        ),
        "files": results["query_structural_records"]["records"],
        "classes": results["find_symbols"]["records"],
        "inspected_paths": [
            {"relative_path": item["relative_path"], "found": item["found"]}
            for item in results["inspect_paths"]["paths"]
        ],
        "currentness_before": before["data"]["snapshot_check"]["currentness"],
        "currentness_after": stale["data"]["snapshot_check"]["currentness"],
        "changed_paths": stale["data"]["snapshot_check"]["mismatch_paths"],
        "same_pinned_snapshot": same_snapshot,
        "after_change_request": after_request,
        "old_snapshot_still_readable": old_records,
        "new_class_in_old_snapshot": False,
    }


def demonstrate(work: Path, raw: dict) -> dict:
    repo = work / "repo"
    package = repo / "shop"
    package.mkdir(parents=True)
    for source in (Path(__file__).parent / "shop").glob("*.py"):
        shutil.copyfile(source, package / source.name)
    hooks = work / "empty-hooks"
    hooks.mkdir()
    env = {
        # Inherited Git controls must not redirect seed writes outside this TEMP repo.
        **{key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "PROJECT_KB_HOME": str(work / "pkb-home"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Project KB Demo",
        "GIT_AUTHOR_EMAIL": "demo@example.invalid",
        "GIT_COMMITTER_NAME": "Project KB Demo",
        "GIT_COMMITTER_EMAIL": "demo@example.invalid",
    }
    git = ["git", "-c", f"core.hooksPath={hooks}", "-c", "core.autocrlf=false",
           "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false"]
    run_command([*git, "init", "--quiet"], repo, env)
    run_command([*git, "add", "shop"], repo, env)
    run_command([*git, "commit", "--quiet", "-m", "Add synthetic order example"], repo, env)

    registered = cli(["register", ALIAS, str(repo)], repo, env, raw)
    indexed = cli(["index", ALIAS, "--full"], repo, env, raw)
    before = cli(["status", ALIAS, "--verify", "strong"], repo, env, raw)
    symbols = cli(["symbols", ALIAS, "--name", "Order"], repo, env, raw)
    imports = cli(["imports", ALIAS, "--file", "shop/services.py"], repo, env, raw)
    inspection = cli(["inspect", ALIAS, "--file", "shop/services.py"], repo, env, raw)
    summary = {
        "register": registered["code"],
        "index": indexed["code"],
        "symbols": [
            select(item, "relative_path", "qualified_name", "start_line", "end_line")
            for item in symbols["data"]["symbols"]
        ],
        "imports": [
            select(item, "relative_path", "module_text", "imported_name",
                   "resolution_status", "resolved_relative_path")
            for item in imports["data"]["imports"]
        ],
        "inspection": inspect_summary(inspection["data"]["inspection"]),
        "mcp_requests": REQUESTS,
        "mcp": asyncio.run(mcp_demo(repo, env, raw, before, work)),
    }
    return summary


def print_summary(summary: dict) -> None:
    mcp = summary["mcp"]
    print("1. CLI: register → index --full → status --verify strong")
    print(f"   {summary['register']} / {summary['index']} / {mcp['currentness_before']}")
    print("\n2. Файлы снимка: query_structural_records(dataset='files')")
    for item in mcp["files"]:
        print(f"   {item['relative_path']} (строк: {item['line_count']})")
    print("\n3. Классы: find_symbols")
    for item in mcp["classes"]:
        print(f"   {item['short_name']}: "
              f"{item['relative_path']}:{item['start_line']}-{item['end_line']}")
    print("\n4. CLI imports: shop/services.py")
    for item in summary["imports"]:
        print(f"   {item['module_text']} → {item['resolved_relative_path']} "
              f"[{item['resolution_status']}]")
    file = summary["inspection"]["file"]
    print("\n5. CLI inspect: "
          f"{file['relative_path']} / {file['parse_status']} / "
          f"{summary['inspection']['diagnostics']} диагностик")
    print("\n6. MCP stdio: все четыре инструмента вызваны в одном сеансе")
    print("\n7. Изменена только временная копия shop/models.py:")
    print(f"   {mcp['currentness_before']} → {mcp['currentness_after']}")
    print("   Старый снимок читается; новый ExpressOrder в нём отсутствует.")
    print("   Актуальность проверена отдельно через strong status, а не через MCP.")


def remove_readonly(function, path, error) -> None:
    """Git objects may be read-only on Windows; retry their removal."""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Короткий переносимый JSON")
    parser.add_argument("--keep-temp", action="store_true", help="Сохранить TEMP для показа")
    args = parser.parse_args()
    temp_parent = Path(tempfile.gettempdir()).resolve()
    work = Path(tempfile.mkdtemp(prefix="pkb-order-demo-", dir=temp_parent)).resolve()
    raw = {"cli": [], "mcp": {}}
    try:
        summary = demonstrate(work, raw)
        (work / "portable.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print_summary(summary)
    finally:
        (work / "transcript.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        if args.keep_temp:
            print(f"\nTEMP сохранён: {work}", file=sys.stderr)
            print("Полные ответы: transcript.json; сводка при успехе: portable.json",
                  file=sys.stderr)
        else:
            # Удаляем только созданный этим запуском каталог непосредственно в TEMP.
            target = work.resolve()
            if target.parent != temp_parent or not target.name.startswith("pkb-order-demo-"):
                raise RuntimeError("Refusing cleanup outside the demo TEMP directory")
            shutil.rmtree(target, onexc=remove_readonly)


if __name__ == "__main__":
    main()
