"""Deterministic candidate/baseline comparison and release-gate evaluation."""

from __future__ import annotations

import math
from decimal import Decimal

from llm_eval_control_plane.domain import (
    AggregateComparison,
    ArtifactRef,
    CaseChange,
    CaseResult,
    CaseResultStatus,
    ComparisonValue,
    ComparisonValueStatus,
    DatasetVersion,
    ErrorObservation,
    EvaluationCase,
    EvaluationSpec,
    GateCaseComparison,
    GateFailureCode,
    GateResult,
    GateStatus,
    MetricAggregate,
    MetricDirection,
    MetricSummary,
    ReleaseDecision,
    RunResult,
    ScoredObservation,
    SkippedObservation,
)
from llm_eval_control_plane.domain.datasets import SliceLabel


class ComparisonConfigurationError(ValueError):
    """A safe preflight failure before release evidence is produced."""


def compare_runs(
    *,
    spec: EvaluationSpec,
    dataset: DatasetVersion,
    baseline: RunResult,
    candidate: RunResult,
) -> ReleaseDecision:
    """Compare two resolved runs and apply every absolute and regression gate."""
    _validate_inputs(
        spec=spec,
        dataset=dataset,
        baseline=baseline,
        candidate=candidate,
    )
    baseline_cases = {case.case_id: case for case in baseline.cases}
    candidate_cases = {case.case_id: case for case in candidate.cases}
    baseline_metrics = _metric_references(baseline)
    candidate_metrics = _metric_references(candidate)
    slices: tuple[SliceLabel | None, ...] = (
        None,
        *sorted({label for case in dataset.cases for label in case.slices}),
    )

    aggregates: list[AggregateComparison] = []
    for metric in sorted(baseline_metrics):
        evaluator = baseline_metrics[metric]
        if candidate_metrics[metric] != evaluator:
            raise ComparisonConfigurationError(
                "candidate and baseline evaluator revisions must match"
            )
        for slice_name in slices:
            selected = _cases_in_scope(dataset.cases, slice_name)
            baseline_aggregate = _aggregate(
                selected,
                baseline_cases,
                metric,
            )
            candidate_aggregate = _aggregate(
                selected,
                candidate_cases,
                metric,
            )
            delta = (
                None
                if baseline_aggregate.mean is None or candidate_aggregate.mean is None
                else _difference(
                    candidate_aggregate.mean,
                    baseline_aggregate.mean,
                )
            )
            aggregates.append(
                AggregateComparison(
                    metric=metric,
                    slice=slice_name,
                    evaluator=evaluator,
                    baseline=baseline_aggregate,
                    candidate=candidate_aggregate,
                    delta=delta,
                )
            )

    aggregate_index = {(item.metric, item.slice): item for item in aggregates}
    _validate_stored_summaries(
        run=baseline,
        recomputed=aggregate_index,
        side="baseline",
    )
    _validate_stored_summaries(
        run=candidate,
        recomputed=aggregate_index,
        side="candidate",
    )

    gate_results: list[GateResult] = []
    case_comparisons: list[GateCaseComparison] = []
    for gate in spec.gates:
        aggregate = aggregate_index.get((gate.metric, gate.slice))
        if aggregate is None:
            raise ComparisonConfigurationError(
                "gate metric or slice is absent from compared evidence"
            )
        coverage_passed = _coverage_passes(aggregate)
        threshold_passed = aggregate.candidate.mean is not None and _passes(
            aggregate.candidate.mean,
            direction=gate.direction,
            threshold=gate.threshold,
        )
        regression_passed = aggregate.delta is not None and _regression_passes(
            aggregate.delta,
            direction=gate.direction,
            allowed_regression=gate.allowed_regression,
        )
        failure_codes: list[GateFailureCode] = []
        if not coverage_passed:
            failure_codes.append(GateFailureCode.COVERAGE)
        if not threshold_passed:
            failure_codes.append(GateFailureCode.THRESHOLD)
        if not regression_passed:
            failure_codes.append(GateFailureCode.REGRESSION)
        gate_results.append(
            GateResult(
                metric=gate.metric,
                slice=gate.slice,
                direction=gate.direction,
                threshold=gate.threshold,
                allowed_regression=gate.allowed_regression,
                aggregate=aggregate,
                coverage_passed=coverage_passed,
                threshold_passed=threshold_passed,
                regression_passed=regression_passed,
                status=(GateStatus.PASSED if not failure_codes else GateStatus.FAILED),
                failure_codes=tuple(failure_codes),
            )
        )
        for case in _cases_in_scope(dataset.cases, gate.slice):
            baseline_value = _case_value(baseline_cases[case.case_id], gate.metric)
            candidate_value = _case_value(candidate_cases[case.case_id], gate.metric)
            case_comparisons.append(
                _compare_case(
                    case=case,
                    metric=gate.metric,
                    slice_name=gate.slice,
                    direction=gate.direction,
                    threshold=gate.threshold,
                    baseline=baseline_value,
                    candidate=candidate_value,
                )
            )

    assert spec.baseline is not None
    return ReleaseDecision.create(
        spec_name=spec.name,
        dataset=dataset.artifact_ref,
        baseline=baseline.target,
        candidate=candidate.target,
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        baseline_result_digest=baseline.result_digest,
        candidate_result_digest=candidate.result_digest,
        aggregates=tuple(aggregates),
        gates=tuple(gate_results),
        cases=tuple(case_comparisons),
    )


def _validate_inputs(
    *,
    spec: EvaluationSpec,
    dataset: DatasetVersion,
    baseline: RunResult,
    candidate: RunResult,
) -> None:
    if spec.baseline is None:
        raise ComparisonConfigurationError(
            "baseline comparison requires a baseline target reference"
        )
    resolved_dataset = dataset.artifact_ref
    for actual in (baseline.dataset, candidate.dataset):
        if actual != resolved_dataset:
            raise ComparisonConfigurationError(
                "candidate and baseline must use the supplied dataset revision"
            )
    _require_artifact_match(spec.dataset, resolved_dataset, "dataset")
    _require_artifact_match(spec.baseline, baseline.target, "baseline target")
    _require_artifact_match(spec.candidate, candidate.target, "candidate target")

    expected_case_ids = tuple(case.case_id for case in dataset.cases)
    if tuple(case.case_id for case in baseline.cases) != expected_case_ids:
        raise ComparisonConfigurationError(
            "baseline cases must exactly match the supplied dataset"
        )
    if tuple(case.case_id for case in candidate.cases) != expected_case_ids:
        raise ComparisonConfigurationError(
            "candidate cases must exactly match the supplied dataset"
        )
    baseline_metrics = _metric_references(baseline)
    candidate_metrics = _metric_references(candidate)
    if set(baseline_metrics) != set(candidate_metrics):
        raise ComparisonConfigurationError(
            "candidate and baseline metric sets must match"
        )


def _require_artifact_match(
    expected: ArtifactRef,
    actual: ArtifactRef,
    role: str,
) -> None:
    digest_matches = expected.digest is None or expected.digest == actual.digest
    if expected.logical_key != actual.logical_key or not digest_matches:
        raise ComparisonConfigurationError(f"{role} does not match evaluation policy")


def _metric_references(run: RunResult) -> dict[str, ArtifactRef]:
    references = {summary.metric: summary.evaluator for summary in run.metrics}
    if len(references) != len(run.metrics):
        raise ComparisonConfigurationError("run metric names must be unique")
    return references


def _cases_in_scope(
    cases: tuple[EvaluationCase, ...],
    slice_name: SliceLabel | None,
) -> tuple[EvaluationCase, ...]:
    selected = (
        cases
        if slice_name is None
        else tuple(case for case in cases if slice_name in case.slices)
    )
    if not selected:
        raise ComparisonConfigurationError("comparison slice contains no cases")
    return selected


def _case_value(case: CaseResult, metric: str) -> ComparisonValue:
    if case.status is CaseResultStatus.TARGET_FAILED:
        return ComparisonValue(status=ComparisonValueStatus.ERROR)
    observation = next(
        (item for item in case.observations if item.metric == metric),
        None,
    )
    if isinstance(observation, ScoredObservation):
        return ComparisonValue(
            status=ComparisonValueStatus.SCORED,
            value=observation.value,
        )
    if isinstance(observation, SkippedObservation):
        return ComparisonValue(status=ComparisonValueStatus.SKIPPED)
    if isinstance(observation, ErrorObservation) or observation is None:
        return ComparisonValue(status=ComparisonValueStatus.ERROR)
    raise AssertionError("unreachable metric observation type")


def _aggregate(
    selected: tuple[EvaluationCase, ...],
    results: dict[str, CaseResult],
    metric: str,
) -> MetricAggregate:
    values: list[float] = []
    skipped = 0
    errors = 0
    for case in selected:
        value = _case_value(results[case.case_id], metric)
        if value.status is ComparisonValueStatus.SCORED:
            assert value.value is not None
            values.append(value.value)
        elif value.status is ComparisonValueStatus.SKIPPED:
            skipped += 1
        else:
            errors += 1
    return MetricAggregate(
        attempted=len(selected),
        scored=len(values),
        skipped=skipped,
        errors=errors,
        mean=math.fsum(values) / len(values) if values else None,
    )


def _validate_stored_summaries(
    *,
    run: RunResult,
    recomputed: dict[tuple[str, SliceLabel | None], AggregateComparison],
    side: str,
) -> None:
    for summary in run.metrics:
        aggregate = recomputed[(summary.metric, None)]
        actual = aggregate.baseline if side == "baseline" else aggregate.candidate
        expected = _aggregate_from_summary(summary)
        if actual != expected:
            raise ComparisonConfigurationError(
                "stored run aggregates do not match case evidence"
            )


def _aggregate_from_summary(summary: MetricSummary) -> MetricAggregate:
    return MetricAggregate(
        attempted=summary.attempted,
        scored=summary.scored,
        skipped=summary.skipped,
        errors=summary.errors,
        mean=summary.mean,
    )


def _coverage_passes(aggregate: AggregateComparison) -> bool:
    baseline = aggregate.baseline
    candidate = aggregate.candidate
    return (
        baseline.errors == 0
        and candidate.errors == 0
        and baseline.scored > 0
        and baseline.scored == candidate.scored
        and baseline.skipped == candidate.skipped
    )


def _passes(
    value: float,
    *,
    direction: MetricDirection,
    threshold: float,
) -> bool:
    if direction is MetricDirection.HIGHER_IS_BETTER:
        return _at_least(value, threshold)
    return _at_most(value, threshold)


def _regression_passes(
    delta: float,
    *,
    direction: MetricDirection,
    allowed_regression: float,
) -> bool:
    if direction is MetricDirection.HIGHER_IS_BETTER:
        return _at_least(delta, -allowed_regression)
    return _at_most(delta, allowed_regression)


def _difference(left: float, right: float) -> float:
    """Subtract decimal renderings to avoid binary boundary artifacts."""
    return float(Decimal(str(left)) - Decimal(str(right)))


def _at_least(value: float, boundary: float) -> bool:
    return value >= boundary or math.isclose(
        value,
        boundary,
        rel_tol=0.0,
        abs_tol=1e-12,
    )


def _at_most(value: float, boundary: float) -> bool:
    return value <= boundary or math.isclose(
        value,
        boundary,
        rel_tol=0.0,
        abs_tol=1e-12,
    )


def _compare_case(
    *,
    case: EvaluationCase,
    metric: str,
    slice_name: SliceLabel | None,
    direction: MetricDirection,
    threshold: float,
    baseline: ComparisonValue,
    candidate: ComparisonValue,
) -> GateCaseComparison:
    comparable = (
        baseline.status is ComparisonValueStatus.SCORED
        and candidate.status is ComparisonValueStatus.SCORED
    )
    if not comparable:
        return GateCaseComparison(
            metric=metric,
            slice=slice_name,
            case_id=case.case_id,
            slices=case.slices,
            baseline=baseline,
            candidate=candidate,
            change=CaseChange.INCOMPARABLE,
        )
    assert baseline.value is not None
    assert candidate.value is not None
    baseline_passed = _passes(
        baseline.value,
        direction=direction,
        threshold=threshold,
    )
    candidate_passed = _passes(
        candidate.value,
        direction=direction,
        threshold=threshold,
    )
    change = {
        (False, False): CaseChange.UNCHANGED_FAILING,
        (False, True): CaseChange.NEWLY_PASSING,
        (True, False): CaseChange.NEWLY_FAILING,
        (True, True): CaseChange.UNCHANGED_PASSING,
    }[(baseline_passed, candidate_passed)]
    return GateCaseComparison(
        metric=metric,
        slice=slice_name,
        case_id=case.case_id,
        slices=case.slices,
        baseline=baseline,
        candidate=candidate,
        delta=_difference(candidate.value, baseline.value),
        baseline_passed=baseline_passed,
        candidate_passed=candidate_passed,
        change=change,
    )
