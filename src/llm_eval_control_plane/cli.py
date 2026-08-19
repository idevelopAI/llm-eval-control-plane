"""Command-line access to control-plane contracts."""

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from llm_eval_control_plane import __version__
from llm_eval_control_plane.domain import EvaluationSpec

app = typer.Typer(
    add_completion=False,
    help="Inspect and validate reproducible AI evaluation contracts.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def version_callback(value: bool) -> None:
    """Print the installed package version and exit."""
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            help="Show the installed version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Work with versioned evaluation specifications."""


@app.command("schema")
def print_schema() -> None:
    """Print the EvaluationSpec JSON Schema."""
    typer.echo(json.dumps(EvaluationSpec.model_json_schema(), indent=2, sort_keys=True))


@app.command()
def validate(
    specification: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """Validate one evaluation specification without executing it."""
    try:
        spec = EvaluationSpec.model_validate_json(specification.read_text())
    except OSError as error:
        typer.echo(f"Could not read specification: {error}", err=True)
        raise typer.Exit(code=2) from error
    except ValidationError as error:
        typer.echo("Invalid evaluation specification:", err=True)
        for detail in error.errors(include_input=False):
            location = ".".join(str(part) for part in detail["loc"])
            typer.echo(f"- {location}: {detail['msg']}", err=True)
        raise typer.Exit(code=2) from error

    typer.echo(f"Valid evaluation specification: {spec.name}")
