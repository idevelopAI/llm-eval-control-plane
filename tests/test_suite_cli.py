from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from typer.testing import CliRunner, Result

from llm_eval_control_plane.adapters import FilesystemRunRepository
from llm_eval_control_plane.adapters.suite_files import read_suite
from llm_eval_control_plane.cli import app
from llm_eval_control_plane.domain import EvaluationSuiteVersion

runner = CliRunner()


@dataclass
class SuiteFiles:
    definition: Path
    dataset: Path
    suite: Path
    store: Path


@pytest.fixture
def suite_files(tmp_path: Path) -> SuiteFiles:
    definition = tmp_path / "definition.json"
    definition.write_text(
        json.dumps(
            {
                "schema_version": "suite-definition/v1",
                "name": "release/core",
                "revision": 1,
                "dataset": {"name": "suite/cases", "revision": 1},
                "evaluators": ["exact_match", "latency"],
                "slices": ["language/en"],
                "gates": [
                    {
                        "metric": "quality.exact_match",
                        "direction": "higher_is_better",
                        "threshold": 1.0,
                    },
                    {
                        "metric": "performance.latency_ms",
                        "direction": "lower_is_better",
                        "threshold": 5.0,
                    },
                ],
            }
        )
    )
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(
                {
                    "case_id": f"case-{number}",
                    "input": {"scenario": "echo", "value": "private-output-sentinel"},
                    "expected": "private-output-sentinel",
                    "slices": ["language/en"],
                }
            )
            for number in (1, 2)
        )
        + "\n"
    )
    return SuiteFiles(definition, dataset, tmp_path / "suite.json", tmp_path / "store")


def build(files: SuiteFiles) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "suite",
            "build",
            str(files.definition),
            str(files.dataset),
            "--output",
            str(files.suite),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "private-output-sentinel" not in result.output
    assert not files.store.exists()
    summary: dict[str, Any] = json.loads(result.stdout)
    return summary


def run(files: SuiteFiles, role: str, *, overrides: Path | None = None) -> Result:
    arguments = [
        "suite",
        "run",
        str(files.suite),
        str(files.dataset),
        "--run-id",
        role,
        "--target-name",
        "fake/release",
        "--target-revision",
        "1" if role == "baseline" else "2",
        "--store",
        str(files.store),
    ]
    if overrides is not None:
        arguments.extend(["--scenario-overrides", str(overrides)])
    return runner.invoke(app, arguments)


def compare(files: SuiteFiles, *options: str) -> Result:
    return runner.invoke(
        app,
        [
            "suite",
            "compare",
            str(files.suite),
            str(files.dataset),
            "--baseline-run",
            "baseline",
            "--candidate-run",
            "candidate",
            "--store",
            str(files.store),
            *options,
        ],
    )


def test_suite_help_and_authoring_schema() -> None:
    help_result = runner.invoke(app, ["suite", "--help"])
    assert help_result.exit_code == 0
    for command in ("schema", "build", "validate", "run", "compare"):
        assert command in help_result.stdout
    schema = json.loads(runner.invoke(app, ["suite", "schema"]).stdout)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "suite-definition/v1"
    assert (
        not {"target", "credentials", "execution", "digest"}
        & schema["properties"].keys()
    )


def test_build_is_canonical_create_only_and_owner_only(suite_files: SuiteFiles) -> None:
    summary = build(suite_files)
    payload = suite_files.suite.read_bytes()
    suite = read_suite(suite_files.suite)
    assert summary["suite"] == suite.artifact_ref.model_dump(mode="json")
    assert summary["execution_mode"] == "offline_mock"
    assert summary["evaluator_count"] == summary["gate_count"] == 2
    assert "gates" not in summary and "evaluators" not in summary
    if os.name != "nt":
        assert suite_files.suite.stat().st_mode & 0o777 == 0o600
    failed = runner.invoke(
        app,
        [
            "suite",
            "build",
            str(suite_files.definition),
            str(suite_files.dataset),
            "--output",
            str(suite_files.suite),
        ],
    )
    assert failed.exit_code == 2
    assert suite_files.suite.read_bytes() == payload
    assert list(suite_files.suite.parent.glob(".suite-*")) == []


def test_build_and_validate_do_not_invoke_targets(
    suite_files: SuiteFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("authoring and validation must not construct a target")

    monkeypatch.setattr(
        "llm_eval_control_plane.suite_cli.DeterministicFakeTarget", forbidden
    )
    build(suite_files)
    result = runner.invoke(
        app, ["suite", "validate", str(suite_files.suite), str(suite_files.dataset)]
    )
    assert result.exit_code == 0, result.output
    assert "private-output-sentinel" not in result.output


def test_build_normalizes_order_and_excludes_author_identity_from_digest(
    suite_files: SuiteFiles,
) -> None:
    original = build(suite_files)
    authored = json.loads(suite_files.definition.read_text())
    authored["evaluators"].reverse()
    authored["gates"].reverse()
    suite_files.definition.write_text(json.dumps(authored))
    old_path = suite_files.suite
    suite_files.suite = old_path.with_name("reordered.json")
    assert build(suite_files) == original
    assert suite_files.suite.read_bytes() == old_path.read_bytes()
    authored["revision"] = 2
    suite_files.definition.write_text(json.dumps(authored))
    suite_files.suite = old_path.with_name("revision-2.json")
    revised = build(suite_files)
    assert revised["suite"]["digest"] == original["suite"]["digest"]
    assert revised["suite"]["revision"] == 2


@pytest.mark.parametrize(
    "change",
    [
        "secret",
        "duplicate_evaluator",
        "unknown_evaluator",
        "boolean_revision",
        "string_threshold",
        "boolean_threshold",
        "unknown_metric",
        "unknown_slice",
        "duplicate_slice",
        "duplicate_gate",
        "too_many_gates",
        "execution",
    ],
)
def test_build_rejects_invalid_definitions_without_artifacts(
    suite_files: SuiteFiles, change: str
) -> None:
    body = json.loads(suite_files.definition.read_text())
    if change == "secret":
        body["api_key"] = "private-sentinel"
    elif change == "duplicate_evaluator":
        body["evaluators"] *= 2
    elif change == "unknown_evaluator":
        body["evaluators"] = ["private-evaluator"]
    elif change == "boolean_revision":
        body["revision"] = True
    elif change == "string_threshold":
        body["gates"][0]["threshold"] = "1.0"
    elif change == "boolean_threshold":
        body["gates"][0]["threshold"] = True
    elif change == "unknown_metric":
        body["gates"][0]["metric"] = "private.metric"
    elif change == "unknown_slice":
        body["slices"] = ["private/slice"]
    elif change == "duplicate_slice":
        body["slices"] *= 2
    elif change == "duplicate_gate":
        body["gates"] *= 2
    elif change == "too_many_gates":
        body["gates"] *= 33
    else:
        body["execution"] = {"adapter": "private-live"}
    suite_files.definition.write_text(json.dumps(body))
    result = runner.invoke(
        app,
        [
            "suite",
            "build",
            str(suite_files.definition),
            str(suite_files.dataset),
            "--output",
            str(suite_files.suite),
        ],
    )
    assert result.exit_code == 2
    assert "private-" not in result.output
    assert not suite_files.suite.exists()
    assert not suite_files.store.exists()


@pytest.mark.parametrize(
    "document",
    [
        '{"name":"one","name":"private-sentinel"}',
        '{"threshold":NaN}',
        "private-sentinel",
        "[" * 1100,
        "x" * (256 * 1024 + 1),
    ],
    ids=["duplicate-key", "non-finite", "malformed", "deep-nesting", "oversized"],
)
def test_suite_documents_are_strict_bounded_json(
    suite_files: SuiteFiles, document: str
) -> None:
    suite_files.definition.write_text(document)
    result = runner.invoke(
        app,
        [
            "suite",
            "build",
            str(suite_files.definition),
            str(suite_files.dataset),
            "--output",
            str(suite_files.suite),
        ],
    )
    assert result.exit_code == 2
    assert "private-sentinel" not in result.output
    assert not suite_files.suite.exists()


def test_suite_lifecycle_is_pinned_reproducible_and_redacted(
    suite_files: SuiteFiles,
) -> None:
    summary = build(suite_files)
    for role in ("baseline", "candidate"):
        result = run(suite_files, role)
        assert result.exit_code == 0, result.output
        assert run(suite_files, role).stdout == result.stdout
        assert json.loads(result.stdout)["suite"] == summary["suite"]
        assert "private-output-sentinel" not in result.output
    first = compare(suite_files)
    assert first.exit_code == 0, first.output
    decision = json.loads(first.stdout)
    assert decision["suite"] == summary["suite"]
    assert decision["execution_mode"] == "offline_mock"
    assert decision["status"] == "passed"
    assert compare(suite_files).stdout == first.stdout
    assert "private-output-sentinel" not in first.output
    shown = runner.invoke(app, ["show", "candidate", "--store", str(suite_files.store)])
    assert json.loads(shown.stdout)["suite"] == summary["suite"]


def test_suite_regression_fails_gate_without_becoming_execution_error(
    suite_files: SuiteFiles,
) -> None:
    build(suite_files)
    assert run(suite_files, "baseline").exit_code == 0
    overrides = suite_files.definition.with_name("overrides.json")
    overrides.write_text('{"case-1":"uppercase"}')
    candidate = run(suite_files, "candidate", overrides=overrides)
    assert candidate.exit_code == 0
    decision = compare(suite_files)
    assert decision.exit_code == 1
    assert json.loads(decision.stdout)["status"] == "failed"
    original = FilesystemRunRepository(suite_files.store).get("candidate")
    conflict = run(suite_files, "candidate")
    assert conflict.exit_code == 2
    assert FilesystemRunRepository(suite_files.store).get("candidate") == original


def test_suite_target_failure_is_persisted_with_exit_one(
    suite_files: SuiteFiles,
) -> None:
    build(suite_files)
    overrides = suite_files.definition.with_name("overrides.json")
    overrides.write_text('{"case-1":"raise"}')
    result = run(suite_files, "candidate", overrides=overrides)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["case_counts"]["target_failed"] == 1
    assert "private-output-sentinel" not in result.output


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "dataset",
        "execution",
        "evaluator",
        "missing_slice",
        "boolean_concurrency",
        "float_concurrency",
    ],
)
def test_run_rejects_suite_tampering_and_drift_before_execution(
    suite_files: SuiteFiles, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(suite_files)
    suite = read_suite(suite_files.suite)
    body = suite.model_dump(mode="json")
    if change == "digest":
        body["digest"] = "sha256:" + "0" * 64
    elif change == "dataset":
        suite_files.dataset.write_text(
            suite_files.dataset.read_text().replace(
                "private-output-sentinel", "different-private-sentinel"
            )
        )
    elif change in {"boolean_concurrency", "float_concurrency"}:
        body["execution"]["max_concurrency"] = (
            True if change == "boolean_concurrency" else 1.0
        )
    else:
        execution = suite.execution
        evaluators = suite.evaluators
        slices = suite.slices
        if change == "execution":
            execution = execution.model_copy(update={"adapter": "unsupported"})
        elif change == "evaluator":
            evaluators = (
                evaluators[0].model_copy(
                    update={
                        "artifact": evaluators[0].artifact.model_copy(
                            update={"revision": 999}
                        )
                    }
                ),
                *evaluators[1:],
            )
        else:
            slices = ("absent/slice",)
        body = EvaluationSuiteVersion.create(
            name=suite.name,
            revision=suite.revision,
            dataset=suite.dataset,
            evaluators=evaluators,
            execution=execution,
            slices=slices,
            gates=suite.gates,
        ).model_dump(mode="json")
    suite_files.suite.write_text(json.dumps(body))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid suite must fail before target construction")

    monkeypatch.setattr(
        "llm_eval_control_plane.suite_cli.DeterministicFakeTarget", forbidden
    )
    result = run(suite_files, "candidate")
    assert result.exit_code == 2, result.output
    assert "private-" not in result.output
    assert not suite_files.store.exists()


def test_compare_rejects_unpinned_and_different_suite_evidence(
    suite_files: SuiteFiles,
) -> None:
    build(suite_files)
    assert run(suite_files, "baseline").exit_code == 0
    legacy = runner.invoke(
        app,
        [
            "run",
            str(suite_files.dataset),
            "--dataset-name",
            "suite/cases",
            "--run-id",
            "candidate",
            "--target-name",
            "fake/release",
            "--target-revision",
            "2",
            "--store",
            str(suite_files.store),
        ],
    )
    assert legacy.exit_code == 0
    assert compare(suite_files).exit_code == 2
    suite_files.store = suite_files.store.with_name("other-store")
    assert run(suite_files, "baseline").exit_code == 0
    suite = read_suite(suite_files.suite)
    suite_files.suite.write_text(
        suite.model_copy(update={"revision": 2}).model_dump_json()
    )
    assert run(suite_files, "candidate").exit_code == 0
    assert compare(suite_files).exit_code == 2


@pytest.mark.parametrize("format", ["json", "markdown", "junit"])
def test_comparison_reports_preserve_pin_and_never_overwrite(
    suite_files: SuiteFiles, format: str
) -> None:
    build(suite_files)
    assert run(suite_files, "baseline").exit_code == 0
    assert run(suite_files, "candidate").exit_code == 0
    output = suite_files.definition.with_name("report.txt")
    report = compare(suite_files, "--format", format, "--output", str(output))
    assert report.exit_code == 0, report.output
    text = output.read_text()
    assert read_suite(suite_files.suite).digest in text
    assert "private-output-sentinel" not in text
    if format == "junit":
        properties = {
            item.attrib["name"]: item.attrib["value"]
            for item in ElementTree.fromstring(text).findall("properties/property")
        }
        assert properties["suite_name"] == "release/core"
    assert compare(suite_files, "--output", str(output)).exit_code == 2
    assert output.read_text() == text


def test_run_rejects_unknown_scenario_cases_and_policy_options(
    suite_files: SuiteFiles,
) -> None:
    build(suite_files)
    overrides = suite_files.definition.with_name("overrides.json")
    overrides.write_text('{"private-case":"echo"}')
    result = run(suite_files, "candidate", overrides=overrides)
    assert result.exit_code == 2
    assert "private-case" not in result.output
    assert not suite_files.store.exists()
    for option in ("--scorer", "--execution-mode", "--spec", "--api-key"):
        rejected = runner.invoke(
            app,
            [
                "suite",
                "run",
                str(suite_files.suite),
                str(suite_files.dataset),
                "--run-id",
                "run",
                option,
                "unused",
            ],
        )
        assert rejected.exit_code == 2


def test_comparison_does_not_resolve_or_invoke_installed_evaluators(
    suite_files: SuiteFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(suite_files)
    assert run(suite_files, "baseline").exit_code == 0
    assert run(suite_files, "candidate").exit_code == 0

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("comparison must use already-pinned evidence")

    monkeypatch.setattr(
        "llm_eval_control_plane.adapters.suite_files.build_evaluators", forbidden
    )
    monkeypatch.setattr("llm_eval_control_plane.suite_cli.build_evaluators", forbidden)
    assert compare(suite_files).exit_code == 0


def test_checked_in_suite_example_detects_the_documented_regression(
    tmp_path: Path,
) -> None:
    examples = Path(__file__).resolve().parents[1] / "examples"
    files = SuiteFiles(
        examples / "release-suite.json",
        examples / "release-gate-40.jsonl",
        tmp_path / "suite.json",
        tmp_path / "store",
    )
    summary = build(files)
    assert summary["gate_count"] == 4
    assert run(files, "baseline").exit_code == 0
    assert (
        run(
            files, "candidate", overrides=examples / "release-regression-overrides.json"
        ).exit_code
        == 0
    )
    result = compare(files)
    assert result.exit_code == 1
    decision = json.loads(result.stdout)
    assert decision["status"] == "failed"
    assert any(gate["status"] == "failed" for gate in decision["gates"])


def test_validate_rejects_corruption_without_echoing_content(
    suite_files: SuiteFiles,
) -> None:
    suite_files.suite.write_text('{"private-sentinel": true}')
    result = runner.invoke(
        app, ["suite", "validate", str(suite_files.suite), str(suite_files.dataset)]
    )
    assert result.exit_code == 2
    assert "private-sentinel" not in result.output
