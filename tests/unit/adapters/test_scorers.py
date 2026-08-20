from pytest import mark, raises

from llm_eval_control_plane.adapters.scorers import (
    DEFAULT_EVALUATOR_KINDS,
    BuiltInEvaluatorKind,
    ExactMatchEvaluator,
    JsonSchemaEvaluator,
    LatencyEvaluator,
    NormalizedMatchEvaluator,
    NumericToleranceEvaluator,
    RefusalEvaluator,
    UsageEvaluator,
    build_evaluators,
    normalize_text,
)
from llm_eval_control_plane.domain import (
    CanonicalJson,
    ErrorObservation,
    EvaluationCase,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
    TargetOutcome,
    TargetResponse,
    TokenUsage,
)


def evaluation_case(
    *,
    expected: object | None = "answer",
    expected_refusal: bool = False,
    expected_schema: object | None = None,
    numeric_tolerance: float | None = None,
) -> EvaluationCase:
    return EvaluationCase(
        case_id="case-1",
        input=CanonicalJson.from_value({"question": "demo"}),
        expected=None if expected is None else CanonicalJson.from_value(expected),
        expected_refusal=expected_refusal,
        expected_schema=(
            None
            if expected_schema is None
            else CanonicalJson.from_value(expected_schema)
        ),
        numeric_tolerance=numeric_tolerance,
    )


def observation(
    output: object = "answer",
    *,
    outcome: TargetOutcome = TargetOutcome.COMPLETED,
    refusal_code: str | None = None,
    latency_ms: float = 5.0,
    input_units: int = 2,
    output_units: int = 1,
) -> TargetObservation:
    return TargetObservation(
        response=TargetResponse(
            output=CanonicalJson.from_value(output),
            outcome=outcome,
            refusal_code=refusal_code,
            usage=TokenUsage(
                input_units=input_units,
                output_units=output_units,
            ),
        ),
        latency_ms=latency_ms,
    )


@mark.parametrize(
    ("actual", "expected", "score"),
    [
        ("answer", "answer", 1.0),
        ("Answer", "answer", 0.0),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, 1.0),
        ([1, 2], [2, 1], 0.0),
    ],
)
def test_exact_match_compares_canonical_json(
    actual: object, expected: object, score: float
) -> None:
    result = ExactMatchEvaluator().evaluate(
        evaluation_case(expected=expected), observation(actual)
    )[0]

    assert isinstance(result, ScoredObservation)
    assert result.value == score


def test_exact_match_skips_missing_expectation() -> None:
    result = ExactMatchEvaluator().evaluate(
        evaluation_case(expected=None), observation()
    )[0]
    assert isinstance(result, SkippedObservation)
    assert result.reason_code == "no_expectation"


@mark.parametrize(
    ("actual", "expected", "score"),
    [
        ("  Straße\n", "STRASSE", 1.0),
        ("Cafe\u0301", "CAFÉ", 1.0),
        ("a\t b", "A B", 1.0),
        ("answer!", "answer", 0.0),
    ],
)
def test_normalized_match_has_explicit_unicode_contract(
    actual: str, expected: str, score: float
) -> None:
    result = NormalizedMatchEvaluator().evaluate(
        evaluation_case(expected=expected), observation(actual)
    )[0]
    assert isinstance(result, ScoredObservation)
    assert result.value == score
    assert normalize_text(normalize_text(actual)) == normalize_text(actual)


def test_normalized_match_reports_non_text_and_missing_expectation() -> None:
    invalid = NormalizedMatchEvaluator().evaluate(
        evaluation_case(expected="1"), observation(1)
    )[0]
    missing = NormalizedMatchEvaluator().evaluate(
        evaluation_case(expected=None), observation("answer")
    )[0]

    assert isinstance(invalid, ErrorObservation)
    assert invalid.error_code == "non_text_value"
    assert isinstance(missing, SkippedObservation)


def test_json_schema_validates_nested_output_and_local_references() -> None:
    schema = {
        "$defs": {
            "item": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
                "additionalProperties": False,
            }
        },
        "type": "array",
        "items": {"$ref": "#/$defs/item"},
        "minItems": 1,
    }
    evaluator = JsonSchemaEvaluator()

    valid = evaluator.evaluate(
        evaluation_case(expected_schema=schema), observation([{"id": 1}])
    )[0]
    invalid = evaluator.evaluate(
        evaluation_case(expected_schema=schema), observation([{"id": "1"}])
    )[0]

    assert isinstance(valid, ScoredObservation) and valid.value == 1.0
    assert isinstance(invalid, ScoredObservation) and invalid.value == 0.0


def test_json_schema_skips_absent_schema_and_rejects_unsafe_configuration() -> None:
    evaluator = JsonSchemaEvaluator()
    missing = evaluator.evaluate(evaluation_case(), observation())[0]
    remote = evaluator.evaluate(
        evaluation_case(expected_schema={"$ref": "https://example.com/schema"}),
        observation(),
    )[0]
    invalid = evaluator.evaluate(
        evaluation_case(expected_schema={"type": 42}), observation()
    )[0]

    assert isinstance(missing, SkippedObservation)
    assert isinstance(remote, ErrorObservation)
    assert remote.error_code == "remote_reference"
    assert isinstance(invalid, ErrorObservation)
    assert invalid.error_code == "invalid_schema"


@mark.parametrize(
    ("actual", "expected", "tolerance", "score"),
    [
        (1, 1, 0, 1.0),
        (1.1, 1.0, 0.1, 1.0),
        (0.1 + 0.2, 0.3, 0.000_000_000_000_001, 1.0),
        (1.101, 1.0, 0.1, 0.0),
    ],
)
def test_numeric_tolerance_uses_decimal_boundaries(
    actual: float, expected: float, tolerance: float, score: float
) -> None:
    result = NumericToleranceEvaluator().evaluate(
        evaluation_case(expected=expected, numeric_tolerance=tolerance),
        observation(actual),
    )[0]

    assert isinstance(result, ScoredObservation)
    assert result.value == score


def test_numeric_tolerance_skips_unconfigured_and_errors_on_wrong_type() -> None:
    evaluator = NumericToleranceEvaluator()
    skipped = evaluator.evaluate(evaluation_case(expected=1), observation(1))[0]
    invalid = evaluator.evaluate(
        evaluation_case(expected=1, numeric_tolerance=0.1), observation("1")
    )[0]

    assert isinstance(skipped, SkippedObservation)
    assert isinstance(invalid, ErrorObservation)
    assert invalid.error_code == "non_numeric_value"


@mark.parametrize(
    ("expected_refusal", "outcome", "score"),
    [
        (True, TargetOutcome.REFUSED, 1.0),
        (True, TargetOutcome.COMPLETED, 0.0),
        (False, TargetOutcome.COMPLETED, 1.0),
        (False, TargetOutcome.REFUSED, 0.0),
    ],
)
def test_refusal_uses_structured_state_not_output_wording(
    expected_refusal: bool, outcome: TargetOutcome, score: float
) -> None:
    result = RefusalEvaluator().evaluate(
        evaluation_case(expected="I cannot", expected_refusal=expected_refusal),
        observation(
            "I cannot",
            outcome=outcome,
            refusal_code="policy_block" if outcome is TargetOutcome.REFUSED else None,
        ),
    )[0]
    assert isinstance(result, ScoredObservation)
    assert result.value == score


def test_latency_and_usage_capture_measured_values() -> None:
    target = observation(latency_ms=12.5, input_units=4, output_units=6)
    latency = LatencyEvaluator().evaluate(evaluation_case(), target)
    usage = UsageEvaluator().evaluate(evaluation_case(), target)

    assert [item.value for item in latency if isinstance(item, ScoredObservation)] == [
        12.5
    ]
    assert [item.value for item in usage if isinstance(item, ScoredObservation)] == [
        4.0,
        6.0,
        10.0,
    ]


def test_evaluator_factory_is_complete_resolved_and_rejects_duplicates() -> None:
    evaluators = build_evaluators()

    assert len(evaluators) == len(DEFAULT_EVALUATOR_KINDS)
    assert {evaluator.ref.name for evaluator in evaluators} == {
        f"builtin/{kind.value}" for kind in BuiltInEvaluatorKind
    }
    assert all(evaluator.ref.digest is not None for evaluator in evaluators)
    with raises(ValueError, match="at least one"):
        build_evaluators(())
    with raises(ValueError, match="must be unique"):
        build_evaluators(
            (BuiltInEvaluatorKind.EXACT_MATCH, BuiltInEvaluatorKind.EXACT_MATCH)
        )
