from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner, Result

from llm_eval_control_plane.adapters.postgres_sandbox import (
    PostgresReplayError,
    PostgresSandboxConfig,
    PsycopgPostgresExecutor,
)
from llm_eval_control_plane.cli import app

PROJECT_ROOT = Path(__file__).parents[2]
EXAMPLE_ROOT = PROJECT_ROOT / "examples" / "databridge"
DATASET = EXAMPLE_ROOT / "cases-v1.jsonl"
FIXTURE_SQL = EXAMPLE_ROOT / "postgres-fixture-v1.sql"
MOCK_RESPONSES = EXAMPLE_ROOT / "mock-responses-v1.json"
REGRESSION_OVERRIDES = EXAMPLE_ROOT / "regression-overrides-v2.json"
RELEASE_POLICY = EXAMPLE_ROOT / "release-policy-v1.json"
FIXTURE_SQL_DIGEST = (
    "sha256:f489a49c66c138c28e64df03c958109755289056bce8ef45e8f4d10649535cd8"
)
FIXTURE_FINGERPRINT = (
    "sha256:e40acff961cc83377391195acb15d09fa2931b1cc9b3dd01ee03fcc043a21a09"
)


def restricted_dsn() -> str:
    dsn = os.environ.get("DATABRIDGE_EVAL_DSN")
    if not dsn:
        pytest.skip("requires a disposable PostgreSQL fixture")
    return dsn


def postgres_executor() -> PsycopgPostgresExecutor:
    return PsycopgPostgresExecutor(
        PostgresSandboxConfig(
            dsn=SecretStr(restricted_dsn()),
            fixture_digest=FIXTURE_SQL_DIGEST,
        )
    )


def test_restricted_role_and_policy_preserve_fixture() -> None:
    dsn = restricted_dsn()
    executor = postgres_executor()
    before = executor.fingerprint_fixture()
    assert before == FIXTURE_FINGERPRINT

    adversarial = json.loads(
        (EXAMPLE_ROOT / "adversarial-sql-v1.json").read_text(encoding="utf-8")
    )
    for case in adversarial["cases"]:
        with pytest.raises(PostgresReplayError) as failure:
            executor.execute(case["sql"])
        assert failure.value.code == "policy_rejected"

    with psycopg.connect(dsn, autocommit=True) as connection:
        read_only = connection.execute("SHOW default_transaction_read_only").fetchone()
        assert read_only == ("on",)
        assert connection.execute("SELECT COUNT(*) FROM employees").fetchone() == (12,)
        with pytest.raises(psycopg.Error):
            connection.execute("UPDATE employees SET salary = 0")
        with pytest.raises(psycopg.Error):
            connection.execute("CREATE TEMP TABLE forbidden (id integer)")

    assert executor.fingerprint_fixture() == before


def databridge_run(
    runner: CliRunner,
    store: Path,
    *,
    run_id: str,
    revision: int,
    regression: bool = False,
) -> Result:
    command = [
        "databridge",
        "run",
        str(DATASET),
        "--run-id",
        run_id,
        "--fixture-sql",
        str(FIXTURE_SQL),
        "--expected-fixture-fingerprint",
        FIXTURE_FINGERPRINT,
        "--responses",
        str(MOCK_RESPONSES),
        "--target-revision",
        str(revision),
        "--store",
        str(store),
    ]
    if regression:
        command.extend(("--response-overrides", str(REGRESSION_OVERRIDES)))
    return runner.invoke(app, command)


def compare_run(
    runner: CliRunner,
    store: Path,
    *,
    candidate: str,
    output: Path,
) -> Result:
    return runner.invoke(
        app,
        [
            "compare",
            str(RELEASE_POLICY),
            str(DATASET),
            "--baseline-run",
            "databridge-baseline",
            "--candidate-run",
            candidate,
            "--store",
            str(store),
            "--format",
            "json",
            "--output",
            str(output),
        ],
    )


def test_offline_release_workflow_passes_and_blocks_seeded_regressions(
    tmp_path: Path,
) -> None:
    restricted_dsn()
    runner = CliRunner()
    store = tmp_path / "evidence"

    baseline = databridge_run(
        runner,
        store,
        run_id="databridge-baseline",
        revision=1,
    )
    passing_candidate = databridge_run(
        runner,
        store,
        run_id="databridge-candidate-pass",
        revision=2,
    )
    regression_candidate = databridge_run(
        runner,
        store,
        run_id="databridge-candidate-regression",
        revision=2,
        regression=True,
    )
    assert baseline.exit_code == 0
    assert passing_candidate.exit_code == 0
    assert regression_candidate.exit_code == 0

    passed_path = tmp_path / "passed.json"
    passed = compare_run(
        runner,
        store,
        candidate="databridge-candidate-pass",
        output=passed_path,
    )
    assert passed.exit_code == 0
    assert json.loads(passed_path.read_text(encoding="utf-8"))["status"] == "passed"

    blocked_path = tmp_path / "blocked.json"
    blocked = compare_run(
        runner,
        store,
        candidate="databridge-candidate-regression",
        output=blocked_path,
    )
    assert blocked.exit_code == 1
    decision = json.loads(blocked_path.read_text(encoding="utf-8"))
    assert decision["status"] == "failed"
    assert {
        (gate["metric"], gate["slice"])
        for gate in decision["gates"]
        if gate["status"] == "failed"
    } == {
        ("interaction.clarification_correct", "expected/clarification"),
        ("interaction.decision_correct", None),
        ("interaction.decision_correct", "language/de"),
        ("safety.unsafe_query_rejection", "expected/refusal"),
        ("sql.read_only_policy", None),
        ("sql.result_set_equivalent", "expected/query"),
    }
    assert {
        case["case_id"]
        for case in decision["cases"]
        if case["change"] == "newly_failing"
    } == {
        "clarification_de_missing_entity",
        "de_gesamtbudget",
        "en_department_count",
        "refusal_en_unsafe_delete",
    }

    stored_evidence = "\n".join(
        path.read_text(encoding="utf-8") for path in store.rglob("*.json")
    )
    for excluded in (
        "Deliberate regression fixture",
        '"answer"',
        '"request_id"',
        '"rows"',
    ):
        assert excluded not in stored_evidence
    assert postgres_executor().fingerprint_fixture() == FIXTURE_FINGERPRINT
