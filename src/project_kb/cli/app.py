"""Typer CLI entrypoint for Project KB."""

from typing import Annotated

import typer

from project_kb.errors import ProjectKbError
from project_kb.exit_codes import OK
from project_kb.output.json import build_error_response, build_response, dumps
from project_kb.output.text import format_capabilities, format_error
from project_kb.registry import RegistryResult, RegistryService
from project_kb.resolver.project import ProjectStatusService
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


def _emit_error(error: ProjectKbError, *, command: str, json_output: bool) -> None:
    envelope = build_error_response(error, command=command)
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
    envelope = build_response(
        ok=outcome.ok,
        result=outcome.result,
        code=outcome.code,
        command="status",
        message=outcome.message,
        data=outcome.data(),
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
    }
    data = {
        "commands": commands,
        "storage": {
            "registry": True,
            "project_database": False,
        },
        "features": {
            "project_resolver": True,
            "git_scanning": False,
            "snapshot_indexing": False,
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
        message="Stage 3 project resolver and registry capabilities are available.",
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


def main() -> None:
    app()
