"""Typer CLI entrypoint for Project KB."""

from typing import Annotated

import typer

from project_kb.errors import NotImplementedFeatureError
from project_kb.exit_codes import NOT_IMPLEMENTED, OK
from project_kb.output.json import build_error_response, build_response, dumps
from project_kb.output.text import format_capabilities, format_error
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


@app.command("version")
def version_command() -> None:
    """Print the Project KB version."""

    typer.echo(__version__)


@app.command()
def status(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a JSON response envelope."),
    ] = False,
) -> None:
    """Show current Project KB status."""

    error = NotImplementedFeatureError()
    data = {"project_state": "NO_REGISTRY"}
    envelope = build_error_response(
        error,
        command="status",
        message="Project resolver is not implemented yet.",
        data=data,
    )

    if json_output:
        _emit_json(envelope, exit_code=NOT_IMPLEMENTED)
        return

    typer.echo("Project resolver is not implemented yet.")
    typer.echo(format_error(error))
    raise typer.Exit(NOT_IMPLEMENTED)


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
        "status": False,
        "capabilities": True,
    }
    data = {
        "commands": commands,
        "storage": {
            "registry": False,
            "project_database": False,
        },
        "features": {
            "project_resolver": False,
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
        message="Stage 1 skeleton capabilities are available.",
        data=data,
    )

    if json_output:
        _emit_json(envelope)
        return

    typer.echo(format_capabilities(commands))


def main() -> None:
    app()
