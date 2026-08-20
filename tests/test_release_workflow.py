import json
from pathlib import Path

from typer.testing import CliRunner, Result

from llm_eval_control_plane.cli import app

PROJECT_ROOT = Path(__file__).parents[1]
DATASET = PROJECT_ROOT / "examples" / "release-gate-40.jsonl"
POLICY = PROJECT_ROOT / "examples" / "release-gate-spec.json"
REGRESSION = PROJECT_ROOT / "examples" / "release-regression-overrides.json"
DATASET_DIGEST = (
    "sha256:0b6717a93970da558d474ba33c7c37bf10d2f3fb8e5888a264f2170cb16a0a31"
)
REGRESSION_DECISION_DIGEST = (
    "sha256:49d78403effb4cd99d0b5d4dd4f393c073f374aa225c9e6a8d600fa1588c2dff"
)


def run_fixture(
    runner: CliRunner,
    store: Path,
    *,
    run_id: str,
    revision: int,
    regression: bool = False,
) -> None:
    command = [
        "run",
        str(DATASET),
        "--run-id",
        run_id,
        "--dataset-name",
        "release-gate/offline",
        "--target-name",
        "fake/release",
        "--target-revision",
        str(revision),
        "--store",
        str(store),
    ]
    if regression:
        command.extend(("--scenario-overrides", str(REGRESSION)))
    result = runner.invoke(app, command)
    assert result.exit_code == 0


def compare_fixture(
    runner: CliRunner,
    store: Path,
    candidate_run: str,
) -> Result:
    return runner.invoke(
        app,
        [
            "compare",
            str(POLICY),
            str(DATASET),
            "--baseline-run",
            "baseline",
            "--candidate-run",
            candidate_run,
            "--store",
            str(store),
        ],
    )


def test_seeded_release_fixture_proves_pass_and_slice_regression(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    run_fixture(runner, tmp_path, run_id="baseline", revision=1)
    run_fixture(runner, tmp_path, run_id="candidate-pass", revision=2)
    run_fixture(
        runner,
        tmp_path,
        run_id="candidate-regression",
        revision=2,
        regression=True,
    )

    passed = compare_fixture(runner, tmp_path, "candidate-pass")
    failed = compare_fixture(runner, tmp_path, "candidate-regression")

    assert passed.exit_code == 0
    assert json.loads(passed.stdout)["status"] == "passed"
    assert failed.exit_code == 1
    decision = json.loads(failed.stdout)
    assert decision["status"] == "failed"
    assert decision["dataset"]["digest"] == DATASET_DIGEST
    assert decision["decision_digest"] == REGRESSION_DECISION_DIGEST
    statuses = {
        (gate["metric"], gate["slice"]): gate["status"] for gate in decision["gates"]
    }
    assert statuses == {
        ("performance.latency_ms", None): "passed",
        ("quality.exact_match", None): "passed",
        ("quality.exact_match", "language/de"): "passed",
        ("safety.refusal_correct", "safety/refusal"): "failed",
    }
    newly_failing = {
        case["case_id"]
        for case in decision["cases"]
        if case["change"] == "newly_failing"
    }
    assert newly_failing == {
        "quality-de-001",
        "quality-en-001",
        "refusal-de-001",
    }
    assert "verified-en-001" not in failed.stdout
