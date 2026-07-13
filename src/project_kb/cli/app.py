"""Typer CLI entrypoint for Project KB."""

from typing import Annotated

import typer

from project_kb.errors import IndexingError, ProjectKbError
from project_kb.exit_codes import OK
from project_kb.indexing.query import QueryService
from project_kb.indexing.service import IndexService
from project_kb.output.json import build_error_response, build_response, dumps
from project_kb.output.text import format_capabilities, format_error
from project_kb.registry import RegistryResult, RegistryService
from project_kb.resolver.project import ProjectStatusService
from project_kb.resolver.state import ProjectState
from project_kb.version import __version__

app = typer.Typer(
    add_completion=False,
    help="Local project knowledge layer for agent-assisted development.",
    no_args_is_help=True,
)


def _emit_json(envelope: dict[str, object], *, exit_code: int = OK) -> None:
    typer.echo(dumps(envelope))
    if exit_code != OK:
        raise typer.Exit(exit_code)


def _emit_error(
    error: ProjectKbError,
    *,
    command: str,
    json_output: bool,
    result: str = "blocked",
) -> None:
    envelope = build_error_response(error, command=command, result=result)
    if json_output:
        _emit_json(envelope, exit_code=error.exit_code)
        return

    typer.echo(format_error(error))
    raise typer.Exit(error.exit_code)


def _result_data(result: RegistryResult) -> dict[str, object]:
    data = dict(result.data)
    if result.project is not None:
        data["project"] = result.project.to_dict()
    return data


@app.command("version")
def version_command() -> None:
    """Print the Project KB version."""

    typer.echo(__version__)


@app.command()
def status(
    project_name: Annotated[
        str | None,
        typer.Argument(help="Registered project name. Omit to resolve the current directory."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Show current Project KB status."""

    outcome = ProjectStatusService().status(project_name)
    warnings = []
    if outcome.project_state is ProjectState.SNAPSHOT_PRESENT_REGISTRY_WARNING:
        warnings.append(
            {
                "code": "PUBLISHED_SNAPSHOT_REGISTRY_OUTCOME_STALE",
                "message": "The published snapshot is newer than registry outcome metadata.",
            }
        )
    elif outcome.project_state is ProjectState.LAST_INDEX_FAILED_PREVIOUS_SNAPSHOT_AVAILABLE:
        warnings.append(
            {
                "code": "PREVIOUS_SNAPSHOT_AVAILABLE_AFTER_FAILED_REFRESH",
                "message": "The latest index failed; the previous snapshot remains available.",
            }
        )
    envelope = build_response(
        ok=outcome.ok,
        result=outcome.result,
        code=outcome.code,
        command="status",
        message=outcome.message,
        data=outcome.data(),
        warnings=warnings,
        error=outcome.error,
    )

    if json_output:
        _emit_json(envelope, exit_code=outcome.exit_code)
        return

    typer.echo(f"{outcome.project_state.value}: {outcome.message}")
    if outcome.recommended_action.command:
        typer.echo(f"Recommended: {outcome.recommended_action.command}")
    if outcome.exit_code != OK:
        raise typer.Exit(outcome.exit_code)


@app.command()
def capabilities(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Show currently available Project KB capabilities."""

    commands = {
        "version": True,
        "status": True,
        "capabilities": True,
        "register": True,
        "projects": True,
        "relink": True,
        "unregister": True,
        "index": True,
        "symbols": True,
        "imports": True,
        "inspect": True,
    }
    data = {
        "commands": commands,
        "storage": {
            "registry": True,
            "project_database": True,
        },
        "features": {
            "project_resolver": True,
            "git_scanning": True,
            "snapshot_indexing": True,
            "python_ast": True,
            "structural_queries": True,
            "search": False,
            "exports": False,
            "context_packs": False,
        },
    }
    envelope = build_response(
        ok=True,
        result="success",
        code="OK",
        command="capabilities",
        message="Project resolver, registry, and structural indexing capabilities are available.",
        data=data,
    )

    if json_output:
        _emit_json(envelope)
        return

    typer.echo(format_capabilities(commands))


@app.command()
def register(
    name: Annotated[str, typer.Argument(help="Project CLI alias.")],
    repo_path: Annotated[str, typer.Argument(help="Path inside the Git repository.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Register a local Git project in the Project KB registry."""

    try:
        result = RegistryService().register(name, repo_path)
    except ProjectKbError as error:
        _emit_error(error, command="register", json_output=json_output)
        return

    envelope = build_response(
        ok=True,
        result="success",
        code=result.code,
        command="register",
        message=result.message,
        data=_result_data(result),
    )
    if json_output:
        _emit_json(envelope)
        return

    typer.echo(result.message)


@app.command("projects")
def projects_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """List registered Project KB projects."""

    try:
        projects = RegistryService().list_projects()
    except ProjectKbError as error:
        _emit_error(error, command="projects", json_output=json_output)
        return

    envelope = build_response(
        ok=True,
        result="success",
        code="OK",
        command="projects",
        message="Registered projects listed.",
        data={"projects": [project.to_dict() for project in projects]},
    )
    if json_output:
        _emit_json(envelope)
        return

    for project in projects:
        typer.echo(f"{project.project_name}: {project.repo_root}")


@app.command()
def relink(
    name: Annotated[str, typer.Argument(help="Registered project CLI alias.")],
    new_repo_path: Annotated[str, typer.Argument(help="New path inside the Git repository.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Relink a registered project to a moved Git repository root."""

    try:
        result = RegistryService().relink(name, new_repo_path)
    except ProjectKbError as error:
        _emit_error(error, command="relink", json_output=json_output)
        return

    envelope = build_response(
        ok=True,
        result="success",
        code=result.code,
        command="relink",
        message=result.message,
        data=_result_data(result),
    )
    if json_output:
        _emit_json(envelope)
        return

    typer.echo(result.message)


@app.command()
def unregister(
    name: Annotated[str, typer.Argument(help="Registered project CLI alias.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm removal from Project KB tracking."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Unregister a project and remove its Project KB storage directory."""

    try:
        result = RegistryService().unregister(name, yes=yes)
    except ProjectKbError as error:
        _emit_error(error, command="unregister", json_output=json_output)
        return

    envelope = build_response(
        ok=True,
        result="success",
        code=result.code,
        command="unregister",
        message=result.message,
        data=_result_data(result),
    )
    if json_output:
        _emit_json(envelope)
        return

    typer.echo(result.message)


@app.command("index")
def index_command(
    project_name: Annotated[
        str | None,
        typer.Argument(help="Registered project name. Omit to resolve the current directory."),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Run the V0 full rebuild engine."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Build and atomically publish a full structural snapshot."""

    try:
        outcome = IndexService().index(
            project_name,
            requested_mode="full" if full else "default",
        )
    except IndexingError as error:
        _emit_error(error, command="index", json_output=json_output, result="failed")
        return
    except ProjectKbError as error:
        _emit_error(error, command="index", json_output=json_output)
        return
    envelope = build_response(
        ok=True,
        result=outcome.result,
        code=outcome.code,
        command="index",
        message=outcome.message,
        data=outcome.data,
        warnings=outcome.warnings,
    )
    if json_output:
        _emit_json(envelope)
        return
    typer.echo(outcome.message)


@app.command("symbols")
def symbols_command(
    project_name: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[str | None, typer.Option("--file")] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="Case-sensitive literal prefix; an empty prefix matches every symbol.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List exact indexed Python symbols."""

    try:
        context, items = QueryService().symbols(project_name, file=file, name=name)
    except ProjectKbError as error:
        _emit_error(error, command="symbols", json_output=json_output)
        return
    warnings = context.pop("query_warnings")
    data = {**context, "symbols": items}
    envelope = build_response(
        ok=True,
        result="success_with_warnings" if warnings else "success",
        code="OK",
        command="symbols",
        message="Symbols listed.",
        data=data,
        warnings=warnings,
    )
    if json_output:
        _emit_json(envelope)
        return
    for item in items:
        location = f"{item['relative_path']}:{item['start_line']}-{item['end_line']}"
        typer.echo(f"{location} {item['qualified_name']}")


@app.command("imports")
def imports_command(
    project_name: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[str | None, typer.Option("--file")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List exact indexed Python imports and bounded resolution status."""

    try:
        context, items = QueryService().imports(project_name, file=file)
    except ProjectKbError as error:
        _emit_error(error, command="imports", json_output=json_output)
        return
    warnings = context.pop("query_warnings")
    data = {**context, "imports": items}
    envelope = build_response(
        ok=True,
        result="success_with_warnings" if warnings else "success",
        code="OK",
        command="imports",
        message="Imports listed.",
        data=data,
        warnings=warnings,
    )
    if json_output:
        _emit_json(envelope)
        return
    for item in items:
        location = f"{item['relative_path']}:{item['start_line']}-{item['end_line']}"
        typer.echo(f"{location} {item['module_text']} [{item['resolution_status']}]")


@app.command("inspect")
def inspect_command(
    project_name: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[str, typer.Option("--file")] = "",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect one indexed file including parse diagnostics."""

    if not file:
        error = ProjectKbError(code="FILE_REQUIRED", message="--file is required.", exit_code=2)
        _emit_error(error, command="inspect", json_output=json_output)
        return
    try:
        context, item = QueryService().inspect(project_name, file=file)
    except ProjectKbError as error:
        _emit_error(error, command="inspect", json_output=json_output)
        return
    warnings = context.pop("query_warnings")
    data = {**context, "inspection": item}
    envelope = build_response(
        ok=True,
        result="success_with_warnings" if warnings else "success",
        code="OK",
        command="inspect",
        message="File inspected.",
        data=data,
        warnings=warnings,
    )
    if json_output:
        _emit_json(envelope)
        return
    file_data = item["file"]
    typer.echo(f"Path: {file_data['relative_path']}")
    typer.echo(f"Analysis: {file_data['analysis_level']}")
    typer.echo(f"Language: {file_data['language'] or '-'}")
    typer.echo(f"Size: {file_data['size_bytes']}")
    typer.echo(f"Lines: {file_data['line_count']}")
    typer.echo(f"Hash: {file_data['content_hash'] or '-'}")
    typer.echo(f"Parse: {file_data['parse_status']}")
    typer.echo(f"Symbols: {len(item['symbols'])}")
    typer.echo(f"Imports: {len(item['imports'])}")
    typer.echo(f"Diagnostics: {len(item['parse_diagnostics'])}")
    if item["parse_diagnostics"]:
        first = item["parse_diagnostics"][0]
        typer.echo(f"First diagnostic: {first['line']}:{first['column']} {first['message']}")


def main() -> None:
    app()
