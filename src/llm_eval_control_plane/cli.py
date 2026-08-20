"""Safe command-line access to deterministic evaluation workflows."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from llm_eval_control_plane import __version__
from llm_eval_control_plane.adapters import (
    BuiltInEvaluatorKind,
    DatasetImportError,
    DeterministicFakeTarget,
    DeterministicStepClock,
    FilesystemRunRepository,
    RunStoreError,
    build_evaluators,
    read_dataset_jsonl,
)
from llm_eval_control_plane.application import (
    InProcessRunner,
    RunnerConfigurationError,
)
from llm_eval_control_plane.domain import (
    ArtifactRef,
    CaseResult,
    EvaluationSpec,
    RunResult,
    RunStatus,
)

app = typer.Typer(
    add_completion=False,
    help="Run and inspect reproducible offline AI evaluations.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

_DEFAULT_SCORERS = (
    BuiltInEvaluatorKind.EXACT_MATCH,
    BuiltInEvaluatorKind.JSON_SCHEMA,
    BuiltInEvaluatorKind.NUMERIC_TOLERANCE,
    BuiltInEvaluatorKind.REFUSAL,
    BuiltInEvaluatorKind.LATENCY,
    BuiltInEvaluatorKind.USAGE,
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
    """Evaluate versioned offline datasets and inspect immutable evidence."""


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
        typer.echo("Could not read evaluation specification", err=True)
        raise typer.Exit(code=2) from error
    except ValidationError as error:
        typer.echo("Invalid evaluation specification:", err=True)
        for detail in error.errors(include_input=False):
            location = ".".join(str(part) for part in detail["loc"])
            typer.echo(f"- {location}: {detail['msg']}", err=True)
        raise typer.Exit(code=2) from error

    typer.echo(f"Valid evaluation specification: {spec.name}")


@app.command("run")
def run_evaluation(
    dataset: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Strict UTF-8 JSONL evaluation dataset.",
        ),
    ],
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="Unique immutable run identifier."),
    ],
    dataset_name: Annotated[
        str,
        typer.Option("--dataset-name", help="Stable logical dataset name."),
    ] = "offline-fixture",
    dataset_revision: Annotated[
        int,
        typer.Option(
            "--dataset-revision",
            min=1,
            help="Positive immutable dataset revision.",
        ),
    ] = 1,
    store: Annotated[
        Path,
        typer.Option(
            "--store",
            file_okay=False,
            help="Local artifact root; stored evidence may contain sensitive data.",
        ),
    ] = Path(".llm-eval"),
    scorers: Annotated[
        list[BuiltInEvaluatorKind] | None,
        typer.Option(
            "--scorer",
            help="Built-in scorer to run; repeat the option to select several.",
        ),
    ] = None,
) -> None:
    """Execute a deterministic, network-free evaluation and persist the result."""
    try:
        dataset_version = read_dataset_jsonl(
            dataset,
            name=dataset_name,
            revision=dataset_revision,
        )
        selected = tuple(scorers) if scorers else _DEFAULT_SCORERS
        result = asyncio.run(
            InProcessRunner(clock=DeterministicStepClock()).run(
                run_id=run_id,
                dataset=dataset_version,
                target=DeterministicFakeTarget(),
                evaluators=build_evaluators(selected),
            )
        )
        FilesystemRunRepository(store).save(result)
    except DatasetImportError as error:
        location = "" if error.line is None else f" at line {error.line}"
        typer.echo(f"Dataset import failed ({error.code}){location}", err=True)
        raise typer.Exit(code=2) from error
    except RunStoreError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    except (OSError, RunnerConfigurationError, ValidationError, ValueError) as error:
        typer.echo("Evaluation could not be completed", err=True)
        raise typer.Exit(code=2) from error

    _print_json(_run_summary(result))
    if result.status is RunStatus.COMPLETED_WITH_FAILURES:
        raise typer.Exit(code=1)


@app.command("show")
def show_run(
    run_id: Annotated[str, typer.Argument(help="Immutable run identifier.")],
    store: Annotated[
        Path,
        typer.Option(
            "--store",
            file_okay=False,
            help="Local artifact root used by the run command.",
        ),
    ] = Path(".llm-eval"),
    case_id: Annotated[
        str | None,
        typer.Option("--case", help="Show evaluator evidence for one case."),
    ] = None,
    include_output: Annotated[
        bool,
        typer.Option(
            "--include-output",
            help="Include target output; it may contain sensitive data.",
        ),
    ] = False,
) -> None:
    """Inspect safe run metadata; target output is opt-in per case."""
    if include_output and case_id is None:
        typer.echo("--include-output requires --case", err=True)
        raise typer.Exit(code=2)
    try:
        result = FilesystemRunRepository(store).get(run_id)
    except RunStoreError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error

    if case_id is None:
        _print_json(_run_summary(result))
        return
    selected_case = next(
        (case for case in result.cases if case.case_id == case_id),
        None,
    )
    if selected_case is None:
        typer.echo("Case was not found in run artifact", err=True)
        raise typer.Exit(code=2)
    _print_json(_case_evidence(selected_case, include_output=include_output))


def _run_summary(result: RunResult) -> dict[str, object]:
    statuses = Counter(case.status.value for case in result.cases)
    return {
        "artifacts": {
            "dataset": _artifact_summary(result.dataset),
            "evaluators": [
                _artifact_summary(evaluator) for evaluator in result.evaluators
            ],
            "target": _artifact_summary(result.target),
        },
        "case_counts": {
            "attempted": len(result.cases),
            "completed": statuses["completed"],
            "completed_with_errors": statuses["completed_with_errors"],
            "target_failed": statuses["target_failed"],
        },
        "dataset_digest": result.dataset.digest,
        "execution_mode": "offline_deterministic_fixture",
        "metrics": [
            {
                "attempted": metric.attempted,
                "errors": metric.errors,
                "mean": metric.mean,
                "metric": metric.metric,
                "scored": metric.scored,
                "skipped": metric.skipped,
            }
            for metric in result.metrics
        ],
        "result_digest": result.result_digest,
        "run_id": result.run_id,
        "schema_version": "run-summary/v1",
        "status": result.status.value,
    }


def _artifact_summary(artifact: ArtifactRef) -> dict[str, object]:
    return {
        "digest": artifact.digest,
        "name": artifact.name,
        "revision": artifact.revision,
    }


def _case_evidence(case: CaseResult, *, include_output: bool) -> dict[str, object]:
    target: dict[str, object] | None = None
    if case.target is not None:
        target = {
            "latency_ms": case.target.latency_ms,
            "outcome": case.target.response.outcome.value,
            "usage": {
                "input_units": case.target.response.usage.input_units,
                "output_units": case.target.response.usage.output_units,
                "total_units": case.target.response.usage.total_units,
            },
        }
        if include_output:
            target["output"] = case.target.response.output.to_value()
    return {
        "case_id": case.case_id,
        "evaluator_failures": [
            {
                "code": failure.code.value,
                "message": failure.message,
                "stage": failure.stage.value,
            }
            for failure in case.evaluator_failures
        ],
        "observations": [
            {
                key: value
                for key, value in observation.model_dump(mode="json").items()
                if key != "evaluator"
            }
            for observation in case.observations
        ],
        "status": case.status.value,
        "target": target,
        "target_failure": (
            None
            if case.target_failure is None
            else {
                "code": case.target_failure.code.value,
                "latency_ms": case.target_failure.latency_ms,
                "message": case.target_failure.message,
                "stage": case.target_failure.stage.value,
            }
        ),
    }


def _print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))
