import math

from pydantic import TypeAdapter, ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    ErrorObservation,
    ExecutionFailure,
    FailureCode,
    FailureStage,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
    TokenUsage,
)
from llm_eval_control_plane.domain.execution import MetricObservation


def evaluator_ref(*, resolved: bool = True) -> ArtifactRef:
    return ArtifactRef(
        kind=ArtifactKind.EVALUATOR,
        name="builtin/exact",
        revision=1,
        digest="sha256:" + "a" * 64 if resolved else None,
    )


def response(**updates: object) -> TargetResponse:
    values: dict[str, object] = {
        "output": CanonicalJson.from_value("answer"),
        "usage": TokenUsage(input_units=2, output_units=1),
    }
    values.update(updates)
    return TargetResponse.model_validate(values)


def test_target_request_never_contains_an_expectation() -> None:
    request = TargetRequest(
        case_id="case-1",
        input=CanonicalJson.from_value({"question": "safe"}),
    )

    assert set(request.model_dump()) == {"case_id", "input"}


@mark.parametrize("value", [-1, 1.5, True])
def test_usage_requires_non_negative_strict_integers(value: object) -> None:
    with raises(ValidationError):
        TokenUsage.model_validate({"input_units": value, "output_units": 0})


def test_usage_calculates_total_units() -> None:
    assert TokenUsage(input_units=2, output_units=3).total_units == 5


def test_target_response_validates_structured_refusal() -> None:
    refused = response(outcome=TargetOutcome.REFUSED, refusal_code="policy_block")
    assert refused.outcome is TargetOutcome.REFUSED

    with raises(ValidationError, match="require a refusal code"):
        response(outcome=TargetOutcome.REFUSED)
    with raises(ValidationError, match="cannot include a refusal code"):
        response(refusal_code="unexpected")


@mark.parametrize("latency", [-1.0, math.inf, math.nan])
def test_target_observation_rejects_invalid_latency(latency: float) -> None:
    with raises(ValidationError):
        TargetObservation(response=response(), latency_ms=latency)


def test_execution_failure_enforces_stage_and_safe_evaluator_reference() -> None:
    evaluator_failure = ExecutionFailure(
        stage=FailureStage.EVALUATOR,
        code=FailureCode.EVALUATOR_EXCEPTION,
        message="Evaluator raised an exception",
        evaluator=evaluator_ref(),
    )
    assert evaluator_failure.retryable is False

    with raises(ValidationError, match="require an evaluator reference"):
        ExecutionFailure(
            stage=FailureStage.EVALUATOR,
            code=FailureCode.EVALUATOR_EXCEPTION,
            message="Evaluator raised an exception",
        )
    with raises(ValidationError, match="cannot include an evaluator"):
        ExecutionFailure(
            stage=FailureStage.TARGET,
            code=FailureCode.TARGET_EXCEPTION,
            message="Target raised an exception",
            evaluator=evaluator_ref(),
        )


def test_metric_observation_union_round_trips_by_discriminator() -> None:
    adapter: TypeAdapter[MetricObservation] = TypeAdapter(MetricObservation)
    observations: tuple[MetricObservation, ...] = (
        ScoredObservation(
            metric="exact_match",
            evaluator=evaluator_ref(),
            value=1.0,
            reason_code="matched",
        ),
        SkippedObservation(
            metric="normalized_match",
            evaluator=evaluator_ref(),
            reason_code="not_text",
        ),
        ErrorObservation(
            metric="numeric_tolerance",
            evaluator=evaluator_ref(),
            error_code="not_numeric",
            message="Target output was not numeric",
        ),
    )

    restored = tuple(
        adapter.validate_json(observation.model_dump_json())
        for observation in observations
    )
    assert restored == observations


def test_observation_requires_a_resolved_evaluator_reference() -> None:
    with raises(ValidationError, match="resolved digest"):
        ScoredObservation(
            metric="exact_match",
            evaluator=evaluator_ref(resolved=False),
            value=1.0,
            reason_code="matched",
        )
