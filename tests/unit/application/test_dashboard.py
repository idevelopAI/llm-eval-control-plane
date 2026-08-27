import asyncio
from datetime import UTC, datetime

from pytest import raises

from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.comparison import compare_runs
from llm_eval_control_plane.application.dashboard import (
    build_release_decision_distributions,
)
from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    EvaluationSpec,
    MetricDirection,
    MetricGate,
)
from llm_eval_control_plane.domain.control_plane import (
    ReleaseDecisionRecord,
    RunRecord,
)

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def _evidence() -> tuple[ReleaseDecisionRecord, RunRecord, RunRecord]:
    dataset = DatasetVersion.create(
        name="dashboard-distributions",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value({"scenario": "echo", "value": "answer"}),
                expected=CanonicalJson.from_value("answer"),
                slices=("focus",),
            ),
            EvaluationCase(
                case_id="case-002",
                input=CanonicalJson.from_value({"scenario": "echo", "value": "answer"}),
                expected=CanonicalJson.from_value("answer"),
                slices=("focus",),
            ),
            EvaluationCase(
                case_id="case-003",
                input=CanonicalJson.from_value(
                    {"scenario": "echo", "value": "unscored"}
                ),
            ),
            EvaluationCase(
                case_id="case-004",
                input=CanonicalJson.from_value(
                    {"scenario": "echo", "value": "failure"}
                ),
                expected=CanonicalJson.from_value("failure"),
            ),
        ),
    )
    executor = DeterministicEvaluationExecutor()
    baseline = asyncio.run(
        executor.execute(
            run_id="run-baseline",
            dataset=dataset,
            target_name="fake/baseline",
            target_revision=1,
            adapter="deterministic_fake",
            evaluator_names=("exact_match",),
            scenario_overrides={"case-004": "raise"},
        )
    )
    candidate = asyncio.run(
        executor.execute(
            run_id="run-candidate",
            dataset=dataset,
            target_name="fake/candidate",
            target_revision=2,
            adapter="deterministic_fake",
            evaluator_names=("exact_match",),
            scenario_overrides={"case-002": "uppercase", "case-004": "raise"},
        )
    )
    decision = compare_runs(
        spec=EvaluationSpec(
            name="dashboard-policy",
            dataset=dataset.artifact_ref,
            baseline=baseline.target,
            candidate=candidate.target,
            gates=(
                MetricGate(
                    metric="quality.exact_match",
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    threshold=0.75,
                    allowed_regression=0.25,
                ),
                MetricGate(
                    metric="quality.exact_match",
                    slice="focus",
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    threshold=0.5,
                    allowed_regression=0.5,
                ),
            ),
        ),
        dataset=dataset,
        baseline=baseline,
        candidate=candidate,
    )
    return (
        ReleaseDecisionRecord(
            decision_id="decision-dashboard",
            decision=decision,
            created_at=NOW,
        ),
        RunRecord(result=baseline, created_at=NOW),
        RunRecord(result=candidate, created_at=NOW),
    )


def test_distributions_are_fixed_bounded_and_coverage_aware() -> None:
    decision, baseline, candidate = _evidence()

    result = build_release_decision_distributions(
        decision_record=decision,
        baseline_record=baseline,
        candidate_record=candidate,
        metric="quality.exact_match",
        gate_slice=None,
    )

    assert result.decision_id == "decision-dashboard"
    assert result.score.baseline.model_dump() == {
        "attempted": 4,
        "scored": 2,
        "skipped": 1,
        "errors": 1,
        "statistics": {
            "sample_count": 2,
            "suppressed": False,
            "minimum": 1.0,
            "p50": 1.0,
            "p95": 1.0,
            "maximum": 1.0,
            "mean": 1.0,
        },
    }
    assert result.score.candidate.statistics.model_dump() == {
        "sample_count": 2,
        "suppressed": False,
        "minimum": 0.0,
        "p50": 0.0,
        "p95": 1.0,
        "maximum": 1.0,
        "mean": 0.5,
    }
    assert result.score.delta.model_dump() == {
        "attempted": 4,
        "compared": 2,
        "incomparable": 2,
        "statistics": {
            "sample_count": 2,
            "suppressed": False,
            "minimum": -1.0,
            "p50": -1.0,
            "p95": 0.0,
            "maximum": 0.0,
            "mean": -0.5,
        },
    }
    assert result.score.delta.statistics.small_sample is True
    for run in (result.baseline, result.candidate):
        assert run.latency_ms.attempted == 4
        assert run.latency_ms.measured == 4
        assert run.latency_ms.unavailable == 0
        assert run.latency_ms.target_failures == 1
        assert run.latency_ms.statistics.sample_count == 4
        assert run.latency_ms.statistics.suppressed is True
        assert run.input_units.statistics.sample_count == 3
        assert run.input_units.unavailable == 1
        assert run.input_units.statistics.suppressed is True
        assert run.output_units.statistics.sample_count == 3
        assert run.total_units.statistics.sample_count == 3


def test_sliced_distributions_scope_operational_evidence_to_gate_cases() -> None:
    decision, baseline, candidate = _evidence()

    result = build_release_decision_distributions(
        decision_record=decision,
        baseline_record=baseline,
        candidate_record=candidate,
        metric="quality.exact_match",
        gate_slice="focus",
    )

    assert result.score.gate_slice == "focus"
    assert result.score.baseline.attempted == 2
    assert result.score.candidate.attempted == 2
    for run in (result.baseline, result.candidate):
        assert run.latency_ms.attempted == 2
        assert run.input_units.attempted == 2
        assert run.latency_ms.statistics.suppressed is True


def test_distributions_reject_missing_gates_and_unpinned_runs() -> None:
    decision, baseline, candidate = _evidence()

    with raises(LookupError, match="gate evidence"):
        build_release_decision_distributions(
            decision_record=decision,
            baseline_record=baseline,
            candidate_record=candidate,
            metric="quality.missing",
            gate_slice=None,
        )

    with raises(ValueError, match="pinned runs"):
        build_release_decision_distributions(
            decision_record=decision,
            baseline_record=candidate,
            candidate_record=baseline,
            metric="quality.exact_match",
            gate_slice=None,
        )
