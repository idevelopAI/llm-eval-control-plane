from __future__ import annotations

from pathlib import Path

from pytest import raises

from llm_eval_control_plane.adapters import read_dataset_jsonl
from llm_eval_control_plane.adapters.databridge_scorer import (
    CLARIFICATION_CORRECT,
    DATABRIDGE_METRICS,
    DECISION_CORRECT,
    SQL_EXECUTION_SUCCESS,
    SQL_EXPECTED_COLUMNS,
    SQL_PARSE_VALID,
    SQL_READ_ONLY_POLICY,
    SQL_RESULT_SET_EQUIVALENT,
    UNSAFE_QUERY_REJECTION,
    DataBridgeSqlEvaluator,
)
from llm_eval_control_plane.adapters.postgres_sandbox import PostgresReplayError
from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    ErrorObservation,
    EvaluationCase,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
    TargetOutcome,
    TargetResponse,
    TokenUsage,
)
from llm_eval_control_plane.domain.sql import (
    SqlBehavior,
    SqlExpectation,
    SqlReplayResult,
    SqlResultOrder,
    SqlScalar,
    SqlTargetOutput,
)

REFERENCE_SQL = "SELECT id FROM employees ORDER BY id"
CANDIDATE_SQL = "SELECT id FROM employees ORDER BY id ASC"
FIXTURE_DIGEST = "sha256:" + ("b" * 64)


class FakeExecutor:
    def __init__(
        self,
        results: dict[str, SqlReplayResult | PostgresReplayError],
        *,
        fixture_digest: str = FIXTURE_DIGEST,
        replay_revision: int = 1,
    ) -> None:
        self.results = results
        self.fixture_digest = fixture_digest
        self.replay_revision = replay_revision
        self.calls: list[str] = []

    @property
    def semantic_config(self) -> dict[str, object]:
        return {
            "executor_schema": "fake-postgres/v1",
            "fixture_digest": self.fixture_digest,
            "replay_revision": self.replay_revision,
        }

    def execute(self, sql: str) -> SqlReplayResult:
        self.calls.append(sql)
        result = self.results[sql]
        if isinstance(result, PostgresReplayError):
            raise result
        return result

    def fingerprint_fixture(self) -> str:
        return self.fixture_digest


def query_expectation(
    *,
    rows: tuple[tuple[SqlScalar, ...], ...] = ((1,), (2,)),
    columns: tuple[str, ...] = ("id",),
    result_order: SqlResultOrder = SqlResultOrder.ORDERED,
    reference_sql: str = REFERENCE_SQL,
) -> SqlExpectation:
    return SqlExpectation(
        behavior=SqlBehavior.QUERY,
        reference_sql=reference_sql,
        expected_columns=columns,
        expected_rows=rows,
        result_order=result_order,
    )


def evaluation_case(
    expectation: SqlExpectation,
    *,
    input_value: object | None = None,
    expected_refusal: bool | None = None,
) -> EvaluationCase:
    return EvaluationCase(
        case_id="case-1",
        input=CanonicalJson.from_value(
            input_value
            if input_value is not None
            else {"chat_history": "", "language": "en", "question": "List IDs"}
        ),
        expected=expectation.to_canonical(),
        expected_refusal=(
            expectation.behavior is SqlBehavior.REFUSAL
            if expected_refusal is None
            else expected_refusal
        ),
    )


def target_observation(output: SqlTargetOutput | CanonicalJson) -> TargetObservation:
    canonical = output if isinstance(output, CanonicalJson) else output.to_canonical()
    kind = None if isinstance(output, CanonicalJson) else output.kind
    refused = kind is SqlBehavior.REFUSAL
    return TargetObservation(
        response=TargetResponse(
            output=canonical,
            outcome=TargetOutcome.REFUSED if refused else TargetOutcome.COMPLETED,
            refusal_code="policy_block" if refused else None,
            usage=TokenUsage(input_units=2, output_units=1),
        ),
        latency_ms=5,
    )


def observations_by_metric(
    evaluator: DataBridgeSqlEvaluator,
    case: EvaluationCase,
    output: SqlTargetOutput | CanonicalJson,
) -> dict[str, ScoredObservation | SkippedObservation | ErrorObservation]:
    observations = evaluator.evaluate(case, target_observation(output))
    assert tuple(item.metric for item in observations) == DATABRIDGE_METRICS
    return {item.metric: item for item in observations}


def successful_executor() -> FakeExecutor:
    result = SqlReplayResult(columns=("id",), rows=((1,), (2,)))
    return FakeExecutor({REFERENCE_SQL: result, CANDIDATE_SQL: result})


def test_query_scores_all_sql_metrics_and_replays_original_source() -> None:
    executor = successful_executor()
    evaluator = DataBridgeSqlEvaluator(executor)
    output = SqlTargetOutput(
        kind=SqlBehavior.QUERY,
        sql_executions=(CANDIDATE_SQL,),
    )

    observations = observations_by_metric(
        evaluator,
        evaluation_case(query_expectation()),
        output,
    )

    for metric in (
        DECISION_CORRECT,
        SQL_PARSE_VALID,
        SQL_READ_ONLY_POLICY,
        SQL_EXECUTION_SUCCESS,
        SQL_EXPECTED_COLUMNS,
        SQL_RESULT_SET_EQUIVALENT,
    ):
        observation = observations[metric]
        assert isinstance(observation, ScoredObservation)
        assert observation.value == 1
    assert isinstance(observations[CLARIFICATION_CORRECT], SkippedObservation)
    assert isinstance(observations[UNSAFE_QUERY_REJECTION], SkippedObservation)
    assert executor.calls == [REFERENCE_SQL, CANDIDATE_SQL]


def test_unordered_comparison_preserves_duplicate_rows() -> None:
    expected_rows = ((1,), (1,), (2,))
    reference = SqlReplayResult(columns=("id",), rows=((2,), (1,), (1,)))
    equivalent = SqlReplayResult(columns=("id",), rows=((1,), (2,), (1,)))
    missing_duplicate = SqlReplayResult(columns=("id",), rows=((1,), (2,)))
    expectation = query_expectation(
        rows=expected_rows,
        result_order=SqlResultOrder.UNORDERED,
    )

    matching = observations_by_metric(
        DataBridgeSqlEvaluator(
            FakeExecutor({REFERENCE_SQL: reference, CANDIDATE_SQL: equivalent})
        ),
        evaluation_case(expectation),
        SqlTargetOutput(
            kind=SqlBehavior.QUERY,
            sql_executions=(CANDIDATE_SQL,),
        ),
    )[SQL_RESULT_SET_EQUIVALENT]
    mismatching = observations_by_metric(
        DataBridgeSqlEvaluator(
            FakeExecutor({REFERENCE_SQL: reference, CANDIDATE_SQL: missing_duplicate})
        ),
        evaluation_case(expectation),
        SqlTargetOutput(
            kind=SqlBehavior.QUERY,
            sql_executions=(CANDIDATE_SQL,),
        ),
    )[SQL_RESULT_SET_EQUIVALENT]

    assert isinstance(matching, ScoredObservation) and matching.value == 1
    assert isinstance(mismatching, ScoredObservation) and mismatching.value == 0


def test_ordered_comparison_and_expected_columns_are_independent() -> None:
    reference = SqlReplayResult(columns=("id",), rows=((1,), (2,)))
    reordered = SqlReplayResult(columns=("id",), rows=((2,), (1,)))
    wrong_columns = SqlReplayResult(columns=("employee_id",), rows=((1,), (2,)))
    output = SqlTargetOutput(
        kind=SqlBehavior.QUERY,
        sql_executions=(CANDIDATE_SQL,),
    )

    order_metrics = observations_by_metric(
        DataBridgeSqlEvaluator(
            FakeExecutor({REFERENCE_SQL: reference, CANDIDATE_SQL: reordered})
        ),
        evaluation_case(query_expectation()),
        output,
    )
    column_metrics = observations_by_metric(
        DataBridgeSqlEvaluator(
            FakeExecutor({REFERENCE_SQL: reference, CANDIDATE_SQL: wrong_columns})
        ),
        evaluation_case(query_expectation()),
        output,
    )

    assert order_metrics[SQL_EXPECTED_COLUMNS].value == 1  # type: ignore[union-attr]
    assert order_metrics[SQL_RESULT_SET_EQUIVALENT].value == 0  # type: ignore[union-attr]
    assert column_metrics[SQL_EXPECTED_COLUMNS].value == 0  # type: ignore[union-attr]
    assert column_metrics[SQL_RESULT_SET_EQUIVALENT].value == 1  # type: ignore[union-attr]


def test_unsafe_candidate_scores_zero_and_never_reaches_executor() -> None:
    executor = successful_executor()
    evaluator = DataBridgeSqlEvaluator(executor)
    unsafe = "DELETE FROM employees"

    observations = observations_by_metric(
        evaluator,
        evaluation_case(query_expectation()),
        SqlTargetOutput(
            kind=SqlBehavior.QUERY,
            sql_executions=(unsafe,),
        ),
    )

    assert observations[SQL_PARSE_VALID].value == 1  # type: ignore[union-attr]
    assert observations[SQL_READ_ONLY_POLICY].value == 0  # type: ignore[union-attr]
    assert observations[SQL_EXECUTION_SUCCESS].value == 0  # type: ignore[union-attr]
    assert unsafe not in executor.calls
    assert executor.calls == [REFERENCE_SQL]


def test_candidate_execution_failure_is_a_score_not_evaluator_error() -> None:
    reference = SqlReplayResult(columns=("id",), rows=((1,), (2,)))
    executor = FakeExecutor(
        {
            REFERENCE_SQL: reference,
            CANDIDATE_SQL: PostgresReplayError("query_failed"),
        }
    )
    observations = observations_by_metric(
        DataBridgeSqlEvaluator(executor),
        evaluation_case(query_expectation()),
        SqlTargetOutput(
            kind=SqlBehavior.QUERY,
            sql_executions=(CANDIDATE_SQL,),
        ),
    )

    for metric in (
        SQL_EXECUTION_SUCCESS,
        SQL_EXPECTED_COLUMNS,
        SQL_RESULT_SET_EQUIVALENT,
    ):
        observation = observations[metric]
        assert isinstance(observation, ScoredObservation)
        assert observation.value == 0
        assert observation.reason_code == "candidate_execution_failed"


def test_broken_reference_is_an_error_and_candidate_is_not_executed() -> None:
    broken_reference = SqlReplayResult(columns=("id",), rows=((999,),))
    executor = FakeExecutor(
        {
            REFERENCE_SQL: broken_reference,
            CANDIDATE_SQL: SqlReplayResult(columns=("id",), rows=((1,), (2,))),
        }
    )

    observations = observations_by_metric(
        DataBridgeSqlEvaluator(executor),
        evaluation_case(query_expectation()),
        SqlTargetOutput(
            kind=SqlBehavior.QUERY,
            sql_executions=(CANDIDATE_SQL,),
        ),
    )

    for metric in (
        SQL_EXECUTION_SUCCESS,
        SQL_EXPECTED_COLUMNS,
        SQL_RESULT_SET_EQUIVALENT,
    ):
        observation = observations[metric]
        assert isinstance(observation, ErrorObservation)
        assert observation.error_code == "broken_reference"
    assert executor.calls == [REFERENCE_SQL]


def test_clarification_uses_accepted_codes_and_scores_no_sql_as_safe() -> None:
    expectation = SqlExpectation(
        behavior=SqlBehavior.CLARIFICATION,
        accepted_clarification_codes=("provider_clarification",),
    )
    executor = FakeExecutor({})
    observations = observations_by_metric(
        DataBridgeSqlEvaluator(executor),
        evaluation_case(expectation),
        SqlTargetOutput(
            kind=SqlBehavior.CLARIFICATION,
            clarification_code="provider_clarification",
        ),
    )

    assert observations[DECISION_CORRECT].value == 1  # type: ignore[union-attr]
    assert observations[CLARIFICATION_CORRECT].value == 1  # type: ignore[union-attr]
    assert observations[SQL_READ_ONLY_POLICY].value == 1  # type: ignore[union-attr]
    assert isinstance(observations[SQL_PARSE_VALID], SkippedObservation)
    assert isinstance(observations[SQL_EXECUTION_SUCCESS], SkippedObservation)
    assert executor.calls == []


def test_refusal_scores_structured_rejection_and_no_sql_as_safe() -> None:
    expectation = SqlExpectation(behavior=SqlBehavior.REFUSAL)
    observations = observations_by_metric(
        DataBridgeSqlEvaluator(FakeExecutor({})),
        evaluation_case(expectation),
        SqlTargetOutput(kind=SqlBehavior.REFUSAL),
    )

    assert observations[DECISION_CORRECT].value == 1  # type: ignore[union-attr]
    assert observations[UNSAFE_QUERY_REJECTION].value == 1  # type: ignore[union-attr]
    assert observations[SQL_READ_ONLY_POLICY].value == 1  # type: ignore[union-attr]
    assert isinstance(observations[SQL_RESULT_SET_EQUIVALENT], SkippedObservation)


def test_refusal_case_detects_unsafe_sql_without_executing_it() -> None:
    unsafe = "SELECT pg_sleep(10)"
    executor = FakeExecutor({})
    observations = observations_by_metric(
        DataBridgeSqlEvaluator(executor),
        evaluation_case(SqlExpectation(behavior=SqlBehavior.REFUSAL)),
        SqlTargetOutput(
            kind=SqlBehavior.QUERY,
            sql_executions=(unsafe,),
        ),
    )

    assert observations[DECISION_CORRECT].value == 0  # type: ignore[union-attr]
    assert observations[UNSAFE_QUERY_REJECTION].value == 0  # type: ignore[union-attr]
    assert observations[SQL_READ_ONLY_POLICY].value == 0  # type: ignore[union-attr]
    assert executor.calls == []


def test_malformed_candidate_scores_zero_without_evaluator_errors() -> None:
    malformed = CanonicalJson.from_value({"kind": "query", "schema_version": "1"})
    observations = observations_by_metric(
        DataBridgeSqlEvaluator(successful_executor()),
        evaluation_case(query_expectation()),
        malformed,
    )

    assert observations[DECISION_CORRECT].value == 0  # type: ignore[union-attr]
    assert observations[SQL_READ_ONLY_POLICY].value == 0  # type: ignore[union-attr]
    assert all(not isinstance(item, ErrorObservation) for item in observations.values())


def test_invalid_expectation_returns_errors_for_every_metric() -> None:
    case = EvaluationCase(
        case_id="case-1",
        input=CanonicalJson.from_value(
            {"chat_history": "", "language": "en", "question": "List IDs"}
        ),
        expected=CanonicalJson.from_value({"behavior": "query"}),
    )
    observations = DataBridgeSqlEvaluator(successful_executor()).evaluate(
        case,
        target_observation(
            SqlTargetOutput(
                kind=SqlBehavior.QUERY,
                sql_executions=(CANDIDATE_SQL,),
            )
        ),
    )

    assert len(observations) == len(DATABRIDGE_METRICS)
    assert all(isinstance(item, ErrorObservation) for item in observations)


def test_dataset_preflight_validates_input_refusal_state_and_reference_policy() -> None:
    evaluator = DataBridgeSqlEvaluator(successful_executor())
    valid = evaluation_case(query_expectation())
    evaluator.preflight_dataset(
        DatasetVersion.create(name="databridge/test", revision=1, cases=(valid,))
    )

    invalid_input = evaluation_case(query_expectation(), input_value={"question": 1})
    with raises(ValueError, match="valid target input"):
        evaluator.preflight_dataset(
            DatasetVersion.create(
                name="databridge/test", revision=1, cases=(invalid_input,)
            )
        )

    inconsistent_refusal = evaluation_case(
        SqlExpectation(behavior=SqlBehavior.REFUSAL),
        expected_refusal=False,
    )
    with raises(ValueError, match="refusal state"):
        evaluator.preflight_dataset(
            DatasetVersion.create(
                name="databridge/test",
                revision=1,
                cases=(inconsistent_refusal,),
            )
        )

    unsafe_reference = evaluation_case(
        query_expectation(reference_sql="SELECT * FROM forbidden_table")
    )
    with raises(ValueError, match="read-only policy"):
        evaluator.preflight_dataset(
            DatasetVersion.create(
                name="databridge/test",
                revision=1,
                cases=(unsafe_reference,),
            )
        )


def test_evaluator_digest_covers_fixture_identity() -> None:
    first = DataBridgeSqlEvaluator(
        FakeExecutor({}, fixture_digest="sha256:" + ("1" * 64))
    )
    second = DataBridgeSqlEvaluator(
        FakeExecutor({}, fixture_digest="sha256:" + ("2" * 64))
    )

    assert first.ref.digest is not None
    assert first.ref.digest != second.ref.digest


def test_evaluator_digest_covers_replay_semantics() -> None:
    first = DataBridgeSqlEvaluator(FakeExecutor({}, replay_revision=1))
    second = DataBridgeSqlEvaluator(FakeExecutor({}, replay_revision=2))

    assert first.ref.digest is not None
    assert first.ref.digest != second.ref.digest


def test_pinned_databridge_dataset_passes_preflight() -> None:
    dataset = read_dataset_jsonl(
        Path(__file__).parents[3] / "examples/databridge/cases-v1.jsonl",
        name="databridge/source",
        revision=1,
    )
    executor = FakeExecutor({})
    evaluator = DataBridgeSqlEvaluator(executor)

    evaluator.preflight_dataset(dataset)

    assert len(dataset.cases) == 56
    assert executor.calls == []
