import math

from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    EvaluationSpec,
    MetricDirection,
    MetricGate,
)


def artifact(kind: ArtifactKind, revision: int = 1) -> ArtifactRef:
    return ArtifactRef(kind=kind, name=f"demo-{kind.value}", revision=revision)


def gate(metric: str = "task.success_rate") -> MetricGate:
    return MetricGate(
        metric=metric,
        direction=MetricDirection.HIGHER_IS_BETTER,
        threshold=0.9,
    )


def valid_spec(**updates: object) -> EvaluationSpec:
    values: dict[str, object] = {
        "name": "databridge-release",
        "dataset": artifact(ArtifactKind.DATASET),
        "candidate": artifact(ArtifactKind.TARGET, revision=2),
        "baseline": artifact(ArtifactKind.TARGET),
        "gates": (gate(),),
    }
    values.update(updates)
    return EvaluationSpec.model_validate(values)


def test_evaluation_spec_round_trips_through_json() -> None:
    spec = valid_spec(
        gates=(
            gate(),
            MetricGate(
                metric="latency.p95_ms",
                direction=MetricDirection.LOWER_IS_BETTER,
                threshold=2_000,
                allowed_regression=100,
            ),
        )
    )

    restored = EvaluationSpec.model_validate_json(spec.model_dump_json())

    assert restored == spec
    assert restored.schema_version == "1"


def test_evaluation_spec_allows_candidate_without_baseline() -> None:
    spec = valid_spec(baseline=None)

    assert spec.baseline is None


def test_evaluation_spec_requires_dataset_reference() -> None:
    with raises(ValidationError, match="dataset must reference a dataset artifact"):
        valid_spec(dataset=artifact(ArtifactKind.TARGET))


def test_evaluation_spec_requires_candidate_target_reference() -> None:
    with raises(ValidationError, match="candidate must reference a target artifact"):
        valid_spec(candidate=artifact(ArtifactKind.DATASET))


def test_evaluation_spec_requires_baseline_target_reference() -> None:
    with raises(ValidationError, match="baseline must reference a target artifact"):
        valid_spec(baseline=artifact(ArtifactKind.PROMPT))


def test_evaluation_spec_rejects_identical_candidate_and_baseline() -> None:
    candidate = artifact(ArtifactKind.TARGET, revision=2)

    with raises(ValidationError, match="must be different revisions"):
        valid_spec(candidate=candidate, baseline=candidate)


def test_evaluation_spec_compares_logical_revisions_not_digest_assertions() -> None:
    candidate = artifact(ArtifactKind.TARGET, revision=2)
    baseline = candidate.model_copy(update={"digest": "sha256:" + "a" * 64})

    with raises(ValidationError, match="must be different revisions"):
        valid_spec(candidate=candidate, baseline=baseline)


def test_evaluation_spec_rejects_duplicate_metric_gates() -> None:
    with raises(ValidationError, match="gate metric names must be unique"):
        valid_spec(gates=(gate(), gate()))


def test_evaluation_spec_requires_at_least_one_gate() -> None:
    with raises(ValidationError, match="at least 1 item"):
        valid_spec(gates=())


def test_metric_gate_rejects_non_finite_thresholds() -> None:
    for value in (math.inf, -math.inf, math.nan):
        with raises(ValidationError, match="finite number"):
            MetricGate(
                metric="quality.score",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=value,
            )


def test_metric_gate_rejects_negative_regression_budget() -> None:
    with raises(ValidationError, match="greater than or equal to 0"):
        MetricGate(
            metric="quality.score",
            direction=MetricDirection.HIGHER_IS_BETTER,
            threshold=0.9,
            allowed_regression=-0.01,
        )


def test_evaluation_spec_is_deeply_frozen() -> None:
    spec = valid_spec()
    field_name = "threshold"

    with raises(ValidationError, match="Instance is frozen"):
        setattr(spec.gates[0], field_name, 0.5)
