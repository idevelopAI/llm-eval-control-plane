"""Shared content-safe presentation helpers for local CLI workflows."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import typer

from llm_eval_control_plane.domain import (
    ArtifactRef,
    CanonicalJsonError,
    RunResult,
    parse_json,
)


def run_summary(result: RunResult) -> dict[str, object]:
    statuses = Counter(case.status.value for case in result.cases)
    summary: dict[str, object] = {
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
        "execution_mode": result.execution_mode.value,
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
    if result.suite is not None:
        summary["suite"] = result.suite.model_dump(mode="json")
    return summary


def _artifact_summary(artifact: ArtifactRef) -> dict[str, object]:
    return {
        "digest": artifact.digest,
        "name": artifact.name,
        "revision": artifact.revision,
    }


def print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def read_scenario_overrides(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        document = parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, CanonicalJsonError) as error:
        raise ValueError("Scenario overrides could not be read") from error
    if not isinstance(document, dict) or any(
        not isinstance(value, str) for value in document.values()
    ):
        raise ValueError("Scenario overrides must map case IDs to scenario names")
    overrides: dict[str, str] = {}
    for key, value in document.items():
        assert isinstance(value, str)  # noqa: S101 - checked above
        overrides[key] = value
    return overrides


def create_report(path: Path, report: str) -> None:
    with path.open("x", encoding="utf-8", newline="") as output:
        output.write(report)
