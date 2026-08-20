import json
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from llm_eval_control_plane import __version__
from llm_eval_control_plane.cli import app

runner = CliRunner()


def valid_specification() -> dict[str, object]:
    return {
        "name": "release-candidate",
        "dataset": {"kind": "dataset", "name": "cases", "revision": 1},
        "candidate": {"kind": "target", "name": "service", "revision": 2},
        "baseline": {"kind": "target", "name": "service", "revision": 1},
        "gates": [
            {
                "metric": "task.success_rate",
                "direction": "higher_is_better",
                "threshold": 0.9,
            }
        ],
    }


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_describes_truthful_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "schema" in result.stdout
    assert "compare" in result.stdout
    assert "run" in result.stdout
    assert "show" in result.stdout
    assert "validate" in result.stdout


def test_schema_command_returns_json_schema() -> None:
    result = runner.invoke(app, ["schema"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["title"] == "EvaluationSpec"
    assert payload["properties"]["schema_version"]["const"] == "1"


def test_validate_accepts_valid_specification(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_specification()))

    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "Valid evaluation specification: release-candidate"
    )


def test_validate_reports_errors_without_echoing_input(tmp_path: Path) -> None:
    spec_path = tmp_path / "invalid.json"
    spec_path.write_text(
        json.dumps(
            {
                **valid_specification(),
                "dataset": {
                    "kind": "target",
                    "name": "secret-sentinel",
                    "revision": 1,
                },
            }
        )
    )

    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 2
    assert "dataset must reference a dataset artifact" in result.stderr
    assert "secret-sentinel" not in result.stderr


def test_validate_reports_malformed_json(tmp_path: Path) -> None:
    spec_path = tmp_path / "broken.json"
    spec_path.write_text("not-json")

    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 2
    assert "Invalid evaluation specification" in result.stderr
    assert "Invalid JSON" in result.stderr


def test_validate_reports_unreadable_file(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_specification()))

    def fail_read_text(self: Path) -> str:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 2
    assert result.stderr.strip() == "Could not read evaluation specification"
    assert "simulated read failure" not in result.stderr


def write_dataset(path: Path, *, value: str = "private-output-sentinel") -> None:
    path.write_text(
        json.dumps(
            {
                "case_id": "case-001",
                "input": {"scenario": "echo", "value": value},
                "expected": value,
                "slices": ["language/en"],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_run_persists_offline_result_and_prints_safe_summary(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    store = tmp_path / "artifacts"
    write_dataset(dataset)

    result = runner.invoke(
        app,
        [
            "run",
            str(dataset),
            "--run-id",
            "run-001",
            "--store",
            str(store),
        ],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["run_id"] == "run-001"
    assert summary["status"] == "completed"
    assert summary["case_counts"] == {
        "attempted": 1,
        "completed": 1,
        "completed_with_errors": 0,
        "target_failed": 0,
    }
    assert summary["execution_mode"] == "offline_deterministic_fixture"
    assert summary["dataset_digest"].startswith("sha256:")
    assert summary["result_digest"].startswith("sha256:")
    assert "private-output-sentinel" not in result.stdout
    assert len(list((store / "runs").glob("*.json"))) == 1

    repeated = runner.invoke(
        app,
        [
            "run",
            str(dataset),
            "--run-id",
            "run-001",
            "--store",
            str(store),
        ],
    )
    assert repeated.exit_code == 0
    assert json.loads(repeated.stdout) == summary


def test_show_keeps_target_output_opt_in_per_case(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    store = tmp_path / "artifacts"
    write_dataset(dataset)
    run_result = runner.invoke(
        app,
        ["run", str(dataset), "--run-id", "run-001", "--store", str(store)],
    )
    assert run_result.exit_code == 0

    summary = runner.invoke(app, ["show", "run-001", "--store", str(store)])
    safe_case = runner.invoke(
        app,
        ["show", "run-001", "--store", str(store), "--case", "case-001"],
    )
    revealed_case = runner.invoke(
        app,
        [
            "show",
            "run-001",
            "--store",
            str(store),
            "--case",
            "case-001",
            "--include-output",
        ],
    )

    assert summary.exit_code == safe_case.exit_code == revealed_case.exit_code == 0
    assert "private-output-sentinel" not in summary.stdout
    assert "private-output-sentinel" not in safe_case.stdout
    assert json.loads(safe_case.stdout)["target"].get("output") is None
    assert json.loads(revealed_case.stdout)["target"]["output"] == (
        "private-output-sentinel"
    )

    missing_case = runner.invoke(
        app,
        ["show", "run-001", "--store", str(store), "--case", "missing-case"],
    )
    assert missing_case.exit_code == 2
    assert missing_case.stderr.strip() == "Case was not found in run artifact"


def test_show_rejects_unsafe_or_missing_selections(tmp_path: Path) -> None:
    store = tmp_path / "artifacts"

    missing_case = runner.invoke(
        app,
        ["show", "missing-run", "--store", str(store), "--include-output"],
    )
    missing_run = runner.invoke(
        app,
        ["show", "missing-run", "--store", str(store)],
    )
    invalid_id = runner.invoke(
        app,
        ["show", "../../private-sentinel", "--store", str(store)],
    )

    assert missing_case.exit_code == 2
    assert "requires --case" in missing_case.stderr
    assert missing_run.exit_code == 2
    assert missing_run.stderr.strip() == "Run artifact was not found"
    assert invalid_id.exit_code == 2
    assert invalid_id.stderr.strip() == "Run ID is invalid"
    assert "private-sentinel" not in invalid_id.stderr


def test_run_reports_dataset_errors_without_echoing_content(tmp_path: Path) -> None:
    dataset = tmp_path / "invalid.jsonl"
    dataset.write_text(
        '{"case_id":"case-001","input":"private-sentinel","unknown":true}\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["run", str(dataset), "--run-id", "run-001", "--store", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert result.stderr.strip() == "Dataset import failed (invalid_case) at line 1"
    assert "private-sentinel" not in result.stderr


def test_run_rejects_duplicate_scorers_without_internal_error(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    write_dataset(dataset)

    result = runner.invoke(
        app,
        [
            "run",
            str(dataset),
            "--run-id",
            "run-001",
            "--store",
            str(tmp_path / "artifacts"),
            "--scorer",
            "exact_match",
            "--scorer",
            "exact_match",
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.strip() == "Evaluation could not be completed"


def test_run_persists_sanitized_failures_and_returns_automation_exit_one(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "failure.jsonl"
    store = tmp_path / "artifacts"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "case-001",
                "input": {"scenario": "raise", "value": "private-sentinel"},
                "expected": "answer",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["run", str(dataset), "--run-id", "failed-run", "--store", str(store)],
    )
    evidence = runner.invoke(
        app,
        ["show", "failed-run", "--store", str(store), "--case", "case-001"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "completed_with_failures"
    assert "private-sentinel" not in result.stdout
    assert evidence.exit_code == 0
    case = json.loads(evidence.stdout)
    assert case["target"] is None
    assert case["target_failure"]["code"] == "target_exception"
    assert "private-sentinel" not in evidence.stdout


def release_policy() -> dict[str, object]:
    return {
        "name": "offline-release-policy",
        "dataset": {
            "kind": "dataset",
            "name": "offline-fixture",
            "revision": 1,
        },
        "baseline": {
            "kind": "target",
            "name": "fake/release",
            "revision": 1,
        },
        "candidate": {
            "kind": "target",
            "name": "fake/release",
            "revision": 2,
        },
        "gates": [
            {
                "metric": "quality.exact_match",
                "direction": "higher_is_better",
                "threshold": 0.5,
                "allowed_regression": 0.1,
            }
        ],
    }


def create_release_runs(
    tmp_path: Path,
    *,
    regressed: bool,
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    dataset = tmp_path / "release.jsonl"
    store = tmp_path / "artifacts"
    policy = tmp_path / "policy.json"
    overrides = tmp_path / "overrides.json"
    write_dataset(dataset)
    policy.write_text(json.dumps(release_policy()), encoding="utf-8")
    overrides.write_text('{"case-001":"mismatch"}', encoding="utf-8")

    common = [
        str(dataset),
        "--dataset-name",
        "offline-fixture",
        "--target-name",
        "fake/release",
        "--store",
        str(store),
    ]
    baseline = runner.invoke(
        app,
        ["run", *common, "--run-id", "baseline", "--target-revision", "1"],
    )
    candidate_args = [
        "run",
        *common,
        "--run-id",
        "candidate",
        "--target-revision",
        "2",
    ]
    if regressed:
        candidate_args.extend(("--scenario-overrides", str(overrides)))
    candidate = runner.invoke(app, candidate_args)
    assert baseline.exit_code == candidate.exit_code == 0
    return policy, dataset, store


def test_compare_returns_json_and_release_exit_codes(tmp_path: Path) -> None:
    policy, dataset, store = create_release_runs(tmp_path, regressed=False)
    command = [
        "compare",
        str(policy),
        str(dataset),
        "--baseline-run",
        "baseline",
        "--candidate-run",
        "candidate",
        "--store",
        str(store),
    ]

    passed = runner.invoke(app, command)

    assert passed.exit_code == 0
    report = json.loads(passed.stdout)
    assert report["status"] == "passed"
    assert report["gates"][0]["status"] == "passed"
    assert "private-output-sentinel" not in passed.stdout

    policy, dataset, store = create_release_runs(
        tmp_path / "regression",
        regressed=True,
    )
    failed = runner.invoke(
        app,
        [
            "compare",
            str(policy),
            str(dataset),
            "--baseline-run",
            "baseline",
            "--candidate-run",
            "candidate",
            "--store",
            str(store),
        ],
    )

    assert failed.exit_code == 1
    report = json.loads(failed.stdout)
    assert report["status"] == "failed"
    assert report["gates"][0]["failure_codes"] == ["threshold", "regression"]
    assert "private-output-sentinel" not in failed.stdout


def test_compare_writes_junit_without_overwriting_existing_report(
    tmp_path: Path,
) -> None:
    policy, dataset, store = create_release_runs(tmp_path, regressed=True)
    report = tmp_path / "release.xml"
    command = [
        "compare",
        str(policy),
        str(dataset),
        "--baseline-run",
        "baseline",
        "--candidate-run",
        "candidate",
        "--store",
        str(store),
        "--format",
        "junit",
        "--output",
        str(report),
    ]

    created = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    assert created.exit_code == 1
    assert created.stdout.strip() == "Created junit release report"
    assert '<testsuite name="offline-release-policy"' in report.read_text()
    assert repeated.exit_code == 2
    assert repeated.stderr.strip() == "Release comparison could not be completed"


def test_run_rejects_invalid_override_document_without_leaking_it(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    overrides = tmp_path / "overrides.json"
    write_dataset(dataset)
    overrides.write_text(
        '{"case-001":"echo","case-001":"private-sentinel"}',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            str(dataset),
            "--run-id",
            "candidate",
            "--scenario-overrides",
            str(overrides),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.strip() == "Evaluation could not be completed"
    assert "private-sentinel" not in result.stderr
