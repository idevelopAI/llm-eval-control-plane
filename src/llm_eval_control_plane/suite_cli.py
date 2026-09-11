"""Local suite composition root: no network client, provider, database, or keys."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from llm_eval_control_plane.adapters import (
    BuiltInEvaluatorKind,
    DeterministicFakeTarget,
    DeterministicStepClock,
    FilesystemRunRepository,
    ReportFormat,
    RunStoreError,
    build_evaluators,
    read_dataset_jsonl,
    render_report,
)
from llm_eval_control_plane.adapters.suite_files import (
    SuiteDefinition,
    read_suite,
    read_suite_definition,
    read_suite_scenarios,
    resolve_suite,
    suite_summary,
    validate_local_suite,
    write_suite,
)
from llm_eval_control_plane.application import InProcessRunner, compare_runs
from llm_eval_control_plane.cli_support import (
    create_report,
    print_json,
    run_summary,
)
from llm_eval_control_plane.domain import (
    DatasetVersion,
    EvaluationSuiteVersion,
    ReleaseStatus,
    RunStatus,
)

app = typer.Typer(
    help="Build, validate, run, and compare pinned offline evaluation suites.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
_InputFile = Annotated[
    Path,
    typer.Argument(exists=True, dir_okay=False, readable=True),
]
_Store = Annotated[
    Path,
    typer.Option(
        "--store", file_okay=False, help="Owner-only local run artifact root."
    ),
]


def _load_inputs(
    suite_file: Path, dataset_file: Path, *, validate_executor: bool = True
) -> tuple[EvaluationSuiteVersion, DatasetVersion]:
    suite = read_suite(suite_file)
    dataset = read_dataset_jsonl(
        dataset_file, name=suite.dataset.name, revision=suite.dataset.revision
    )
    if validate_executor:
        validate_local_suite(suite, dataset)
    return suite, dataset


@app.command("schema")
def schema() -> None:
    """Print the authoring definition schema, not a resolved suite document."""
    print_json(SuiteDefinition.model_json_schema())


@app.command("build")
def build(
    definition: _InputFile,
    dataset: _InputFile,
    output: Annotated[
        Path,
        typer.Option(
            "--output", dir_okay=False, help="Create a new canonical suite file."
        ),
    ],
) -> None:
    """Resolve dataset and evaluator identities; publish a create-only suite file."""
    try:
        authored = read_suite_definition(definition)
        resolved_dataset = read_dataset_jsonl(
            dataset, name=authored.dataset.name, revision=authored.dataset.revision
        )
        suite = resolve_suite(authored, resolved_dataset)
        write_suite(output, suite)
    except (OSError, ValueError, RunStoreError):
        typer.echo(
            "Suite could not be built; check the inputs and unused output path",
            err=True,
        )
        raise typer.Exit(code=2) from None
    print_json(suite_summary(suite))


@app.command("validate")
def validate(suite_file: _InputFile, dataset: _InputFile) -> None:
    """Verify digest, exact dataset, slices, and installed evaluator contract."""
    try:
        suite, _ = _load_inputs(suite_file, dataset)
    except (OSError, ValueError):
        typer.echo("Suite failed integrity or dependency validation", err=True)
        raise typer.Exit(code=2) from None
    print_json(suite_summary(suite))


@app.command("run")
def run(
    suite_file: _InputFile,
    dataset: _InputFile,
    run_id: Annotated[
        str, typer.Option("--run-id", help="Create-once run identifier.")
    ],
    target_name: Annotated[str, typer.Option("--target-name")] = "fake/deterministic",
    target_revision: Annotated[int, typer.Option("--target-revision", min=1)] = 1,
    store: _Store = Path(".llm-eval"),
    scenario_overrides: Annotated[
        Path | None,
        typer.Option(
            "--scenario-overrides",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Target scenarios only; cannot replace suite policy.",
        ),
    ] = None,
) -> None:
    """Run the pinned protocol with a deterministic target and synthetic metrics."""
    try:
        suite, resolved_dataset = _load_inputs(suite_file, dataset)
        overrides = read_suite_scenarios(scenario_overrides)
        if not set(overrides) <= {case.case_id for case in resolved_dataset.cases}:
            raise ValueError("scenario overrides contain unknown cases")
        result = asyncio.run(
            InProcessRunner(clock=DeterministicStepClock()).run(
                run_id=run_id,
                dataset=resolved_dataset,
                target=DeterministicFakeTarget(
                    name=target_name,
                    revision=target_revision,
                    scenario_overrides=overrides,
                ),
                evaluators=build_evaluators(
                    tuple(BuiltInEvaluatorKind(name) for name in suite.evaluator_names)
                ),
                execution_mode=suite.execution.execution_mode,
                suite=suite,
            )
        )
        FilesystemRunRepository(store).save(result)
    except (OSError, ValueError, RunStoreError):
        typer.echo("Suite evaluation could not be completed", err=True)
        raise typer.Exit(code=2) from None
    print_json(run_summary(result))
    if result.status is RunStatus.COMPLETED_WITH_FAILURES:
        raise typer.Exit(code=1)


@app.command("compare")
def compare(
    suite_file: _InputFile,
    dataset: _InputFile,
    baseline_run: Annotated[str, typer.Option("--baseline-run")],
    candidate_run: Annotated[str, typer.Option("--candidate-run")],
    store: _Store = Path(".llm-eval"),
    report_format: Annotated[
        ReportFormat, typer.Option("--format")
    ] = ReportFormat.JSON,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Apply only the pinned gates to two exact suite-backed local runs."""
    try:
        # Comparisons consume immutable evidence; no installed scorer is invoked.
        suite, resolved_dataset = _load_inputs(
            suite_file, dataset, validate_executor=False
        )
        repository = FilesystemRunRepository(store)
        baseline = repository.get(baseline_run)
        candidate = repository.get(candidate_run)
        decision = compare_runs(
            suite=suite,
            spec=suite.to_evaluation_spec(
                baseline=baseline.target, candidate=candidate.target
            ),
            dataset=resolved_dataset,
            baseline=baseline,
            candidate=candidate,
        )
        report = render_report(decision, report_format)
        if output is None:
            typer.echo(report, nl=False)
        else:
            create_report(output, report)
            typer.echo(f"Created {report_format.value} release report")
    except (OSError, ValueError, RunStoreError):
        typer.echo("Suite comparison could not be completed", err=True)
        raise typer.Exit(code=2) from None
    if decision.status is ReleaseStatus.FAILED:
        raise typer.Exit(code=1)
