from __future__ import annotations

import json
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from llm_eval_control_plane.adapters import FilesystemRunRepository
from llm_eval_control_plane.adapters.databridge import (
    DataBridgeHttpTarget,
    DataBridgeMockTarget,
)
from llm_eval_control_plane.adapters.postgres_sandbox import (
    PsycopgPostgresExecutor,
)
from llm_eval_control_plane.cli import app
from llm_eval_control_plane.domain import (
    CanonicalJson,
    ExecutionMode,
    SqlReplayResult,
    TargetRequest,
    TargetResponse,
    TokenUsage,
)

runner = CliRunner()
FIXTURE_FINGERPRINT = f"sha256:{'1' * 64}"


def write_query_fixture(root: Path) -> tuple[Path, Path, Path]:
    dataset = root / "cases.jsonl"
    responses = root / "responses.json"
    fixture_sql = root / "fixture.sql"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "query-en",
                "expected": {
                    "behavior": "query",
                    "expected_columns": ["value"],
                    "expected_rows": [[1]],
                    "reference_sql": "SELECT 1 AS value",
                    "result_order": "ordered",
                    "schema_version": "1",
                },
                "input": {
                    "chat_history": "",
                    "language": "en",
                    "question": "Return the fixture value.",
                },
                "slices": [
                    "ambiguity/clear",
                    "expected/query",
                    "language/en",
                    "safety/safe",
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    responses.write_text(
        json.dumps(
            {
                "responses": {
                    "query-en": {
                        "body": {
                            "answer": "private-answer-sentinel",
                            "duration_ms": 7,
                            "executions": [
                                {
                                    "columns": ["private-column-sentinel"],
                                    "duration_ms": 2,
                                    "row_count": 1,
                                    "rows": [["private-row-sentinel"]],
                                    "sql": "SELECT 1 AS value",
                                    "truncated": False,
                                }
                            ],
                            "input_tokens": 3,
                            "model_duration_ms": 4,
                            "output_tokens": 2,
                            "request_id": "private-request-sentinel",
                            "status": "answered",
                            "tool_call_count": 1,
                        },
                        "status": 200,
                    }
                },
                "schema_version": "1",
            }
        ),
        encoding="utf-8",
    )
    fixture_sql.write_text("SELECT 'synthetic fixture';\n", encoding="utf-8")
    return dataset, responses, fixture_sql


def command(
    dataset: Path,
    fixture_sql: Path,
    store: Path,
    *,
    run_id: str = "databridge-test",
) -> list[str]:
    return [
        "databridge",
        "run",
        str(dataset),
        "--run-id",
        run_id,
        "--fixture-sql",
        str(fixture_sql),
        "--expected-fixture-fingerprint",
        FIXTURE_FINGERPRINT,
        "--store",
        str(store),
    ]


def replay_success(
    _executor: PsycopgPostgresExecutor,
    _sql: str,
) -> SqlReplayResult:
    return SqlReplayResult(columns=("value",), rows=((1,),))


def fingerprint_success(_executor: PsycopgPostgresExecutor) -> str:
    return FIXTURE_FINGERPRINT


def exception_chain_text(error: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(repr(current))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def test_databridge_mock_command_persists_safe_offline_evidence(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset, responses, fixture_sql = write_query_fixture(tmp_path)
    store = tmp_path / "store"
    monkeypatch.setenv("DATABRIDGE_EVAL_DSN", "private-dsn-sentinel")
    monkeypatch.setattr(PsycopgPostgresExecutor, "execute", replay_success)
    monkeypatch.setattr(
        PsycopgPostgresExecutor,
        "fingerprint_fixture",
        fingerprint_success,
    )

    result = runner.invoke(
        app,
        [*command(dataset, fixture_sql, store), "--responses", str(responses)],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["execution_mode"] == "offline_mock"
    assert summary["case_counts"]["attempted"] == 1
    assert summary["status"] == "completed"
    assert all(metric["errors"] == 0 for metric in summary["metrics"])
    for secret in (
        "private-answer-sentinel",
        "private-column-sentinel",
        "private-dsn-sentinel",
        "private-request-sentinel",
        "private-row-sentinel",
    ):
        assert secret not in result.stdout
        assert secret not in result.stderr

    stored = FilesystemRunRepository(store).get("databridge-test")
    assert stored.execution_mode is ExecutionMode.OFFLINE_MOCK
    serialized = stored.model_dump_json()
    for secret in (
        "private-answer-sentinel",
        "private-column-sentinel",
        "private-dsn-sentinel",
        "private-request-sentinel",
        "private-row-sentinel",
    ):
        assert secret not in serialized


def test_databridge_live_command_requires_opt_ins_and_uses_live_evidence(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset, _responses, fixture_sql = write_query_fixture(tmp_path)
    store = tmp_path / "store"
    monkeypatch.setenv("DATABRIDGE_EVAL_DSN", "private-dsn-sentinel")
    monkeypatch.setenv("DATABRIDGE_API_KEY", "private-api-sentinel")
    monkeypatch.setattr(PsycopgPostgresExecutor, "execute", replay_success)
    monkeypatch.setattr(
        PsycopgPostgresExecutor,
        "fingerprint_fixture",
        fingerprint_success,
    )

    async def live_success(
        _target: DataBridgeHttpTarget,
        _request: TargetRequest,
    ) -> TargetResponse:
        return TargetResponse(
            output=CanonicalJson.from_value(
                {
                    "kind": "query",
                    "schema_version": "1",
                    "sql_executions": ["SELECT 1 AS value"],
                }
            ),
            usage=TokenUsage(input_units=3, output_units=2),
        )

    monkeypatch.setattr(DataBridgeHttpTarget, "invoke", live_success)

    missing_opt_ins = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, store),
            "--live-base-url",
            "https://databridge.example",
        ],
    )
    live = runner.invoke(
        app,
        [
            *command(
                dataset,
                fixture_sql,
                store,
                run_id="databridge-live-test",
            ),
            "--live-base-url",
            "https://databridge.example",
            "--allow-live",
            "--confirm-synthetic-database",
        ],
    )

    assert missing_opt_ins.exit_code == 2
    assert missing_opt_ins.stderr.strip() == (
        "DataBridge evaluation could not be completed"
    )
    assert live.exit_code == 0
    assert json.loads(live.stdout)["execution_mode"] == "live"
    assert "private-api-sentinel" not in live.stdout + live.stderr
    assert "private-dsn-sentinel" not in live.stdout + live.stderr


def test_databridge_command_rejects_missing_or_malformed_secret_configuration(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset, responses, fixture_sql = write_query_fixture(tmp_path)
    monkeypatch.delenv("DATABRIDGE_EVAL_DSN", raising=False)
    missing_dsn = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "store"),
            "--responses",
            str(responses),
        ],
    )

    monkeypatch.setenv("DATABRIDGE_EVAL_DSN", "private-dsn-sentinel")
    monkeypatch.setattr(
        PsycopgPostgresExecutor,
        "fingerprint_fixture",
        fingerprint_success,
    )
    missing_api_key = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "live-store"),
            "--live-base-url",
            "https://databridge.example",
            "--allow-live",
            "--confirm-synthetic-database",
        ],
    )

    duplicate_manifest = tmp_path / "duplicate.json"
    duplicate_manifest.write_text(
        '{"responses":{},"responses":{"private-sentinel":{}},"schema_version":"1"}',
        encoding="utf-8",
    )
    duplicate = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "duplicate-store"),
            "--responses",
            str(duplicate_manifest),
        ],
    )

    for result in (missing_dsn, missing_api_key, duplicate):
        assert result.exit_code == 2
        assert result.stderr.strip() == ("DataBridge evaluation could not be completed")
        assert "private" not in result.stdout + result.stderr


def test_databridge_preflight_rejects_refusal_flag_mismatch_before_target(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset, responses, fixture_sql = write_query_fixture(tmp_path)
    record = json.loads(dataset.read_text(encoding="utf-8"))
    record["expected"] = {"behavior": "refusal", "schema_version": "1"}
    record["expected_refusal"] = False
    dataset.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setenv("DATABRIDGE_EVAL_DSN", "private-dsn-sentinel")

    async def must_not_invoke(
        _target: DataBridgeMockTarget,
        _request: TargetRequest,
    ) -> object:
        raise AssertionError("target must not be invoked after failed preflight")

    monkeypatch.setattr(DataBridgeMockTarget, "invoke", must_not_invoke)
    result = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "store"),
            "--responses",
            str(responses),
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.strip() == "DataBridge evaluation could not be completed"


def test_databridge_live_rejects_shared_secret_reference_before_reading_it(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset, _responses, fixture_sql = write_query_fixture(tmp_path)
    secret = "private-shared-dsn-and-api-sentinel"
    monkeypatch.setenv("SHARED_SECRET", secret)

    result = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "store"),
            "--live-base-url",
            "https://databridge.example",
            "--allow-live",
            "--confirm-synthetic-database",
            "--api-key-env",
            "SHARED_SECRET",
            "--database-dsn-env",
            "SHARED_SECRET",
        ],
    )

    assert result.exit_code == 2
    assert result.stderr.strip() == "DataBridge evaluation could not be completed"
    assert secret not in result.stdout + result.stderr
    assert result.exception is not None
    assert secret not in exception_chain_text(result.exception)


def test_databridge_mock_rejects_unbounded_or_misaligned_manifests(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset, _responses, fixture_sql = write_query_fixture(tmp_path)
    monkeypatch.setenv("DATABRIDGE_EVAL_DSN", "private-dsn-sentinel")
    monkeypatch.setattr(
        PsycopgPostgresExecutor,
        "fingerprint_fixture",
        fingerprint_success,
    )

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b"x" * (4 * 1_024 * 1_024))
    oversized_result = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "oversized-store"),
            "--responses",
            str(oversized),
        ],
    )

    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        json.dumps(
            {
                "responses": {"unknown-case": {"status": 403}},
                "schema_version": "1",
            }
        ),
        encoding="utf-8",
    )
    unknown_result = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "unknown-store"),
            "--responses",
            str(unknown),
        ],
    )

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b'{"responses":{"private-byte-sentinel":\xff}}')
    invalid_utf8_result = runner.invoke(
        app,
        [
            *command(dataset, fixture_sql, tmp_path / "invalid-utf8-store"),
            "--responses",
            str(invalid_utf8),
        ],
    )

    for result in (oversized_result, unknown_result, invalid_utf8_result):
        assert result.exit_code == 2
        assert result.stderr.strip() == ("DataBridge evaluation could not be completed")
        assert result.exception is not None
        chain = exception_chain_text(result.exception)
        assert "private-byte-sentinel" not in chain
        assert "private-dsn-sentinel" not in chain
