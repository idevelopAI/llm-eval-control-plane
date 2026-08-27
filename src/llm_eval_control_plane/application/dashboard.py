"""Derive bounded dashboard statistics from validated immutable evidence."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Literal

from llm_eval_control_plane.domain.analytics import (
    MIN_OPERATIONAL_AGGREGATE_SIZE,
    DeltaDistribution,
    MeasurementDistribution,
    QuantileSummary,
    ReleaseDecisionDistributions,
    RunOperationalDistribution,
    ScoreDistribution,
    ScoreValueDistribution,
)
from llm_eval_control_plane.domain.comparison import (
    ComparisonValue,
    ComparisonValueStatus,
    GateCaseComparison,
)
from llm_eval_control_plane.domain.control_plane import (
    ReleaseDecisionRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.results import CaseResultStatus


def build_release_decision_distributions(
    *,
    decision_record: ReleaseDecisionRecord,
    baseline_record: RunRecord,
    candidate_record: RunRecord,
    metric: str,
    gate_slice: str | None,
) -> ReleaseDecisionDistributions:
    """Build fixed quantiles while discarding every underlying sample value."""
    decision = decision_record.decision
    baseline = baseline_record.result
    candidate = candidate_record.result
    if (
        baseline.run_id != decision.baseline_run_id
        or candidate.run_id != decision.candidate_run_id
        or baseline.result_digest != decision.baseline_result_digest
        or candidate.result_digest != decision.candidate_result_digest
        or baseline.dataset != decision.dataset
        or candidate.dataset != decision.dataset
        or baseline.target != decision.baseline
        or candidate.target != decision.candidate
        or baseline.execution_mode is not decision.execution_mode
        or candidate.execution_mode is not decision.execution_mode
    ):
        raise ValueError("release decision evidence does not match its pinned runs")

    gate_exists = any(
        gate.metric == metric and gate.slice == gate_slice for gate in decision.gates
    )
    cases = tuple(
        item
        for item in decision.cases
        if item.metric == metric and item.slice == gate_slice
    )
    if not gate_exists or not cases:
        raise LookupError("release decision gate evidence was not found")
    case_ids = frozenset(item.case_id for item in cases)

    return ReleaseDecisionDistributions(
        decision_id=decision_record.decision_id,
        score=_score_distribution(cases, metric=metric, gate_slice=gate_slice),
        baseline=_operational_distribution(
            baseline_record,
            role="baseline",
            case_ids=case_ids,
        ),
        candidate=_operational_distribution(
            candidate_record,
            role="candidate",
            case_ids=case_ids,
        ),
    )


def _score_distribution(
    cases: tuple[GateCaseComparison, ...],
    *,
    metric: str,
    gate_slice: str | None,
) -> ScoreDistribution:
    baseline_values = tuple(item.baseline for item in cases)
    candidate_values = tuple(item.candidate for item in cases)
    deltas = tuple(item.delta for item in cases if item.delta is not None)
    return ScoreDistribution(
        metric=metric,
        gate_slice=gate_slice,
        baseline=_score_values(baseline_values),
        candidate=_score_values(candidate_values),
        delta=DeltaDistribution(
            attempted=len(cases),
            compared=len(deltas),
            incomparable=len(cases) - len(deltas),
            statistics=_quantiles(deltas),
        ),
    )


def _score_values(values: tuple[ComparisonValue, ...]) -> ScoreValueDistribution:
    scored: list[float] = []
    skipped = 0
    errors = 0
    for value in values:
        if value.status is ComparisonValueStatus.SCORED:
            if value.value is None:
                raise ValueError("scored comparison evidence is invalid")
            scored.append(float(value.value))
        elif value.status is ComparisonValueStatus.SKIPPED:
            skipped += 1
        elif value.status is ComparisonValueStatus.ERROR:
            errors += 1
        else:
            raise ValueError("comparison evidence status is invalid")
    return ScoreValueDistribution(
        attempted=len(values),
        scored=len(scored),
        skipped=skipped,
        errors=errors,
        statistics=_quantiles(scored),
    )


def _operational_distribution(
    record: RunRecord,
    *,
    role: Literal["baseline", "candidate"],
    case_ids: frozenset[str],
) -> RunOperationalDistribution:
    result = record.result
    cases = tuple(case for case in result.cases if case.case_id in case_ids)
    if len(cases) != len(case_ids):
        raise ValueError("release gate cases do not match pinned run evidence")
    latency: list[float] = []
    input_units: list[float] = []
    output_units: list[float] = []
    total_units: list[float] = []
    target_failures = 0
    latency_unavailable = 0
    usage_unavailable = 0
    for case in cases:
        if case.status is CaseResultStatus.TARGET_FAILED:
            target_failures += 1
            usage_unavailable += 1
            if case.target_failure is None or case.target_failure.latency_ms is None:
                latency_unavailable += 1
            else:
                latency.append(float(case.target_failure.latency_ms))
            continue
        if case.target is None:
            latency_unavailable += 1
            usage_unavailable += 1
            continue
        usage = case.target.response.usage
        latency.append(float(case.target.latency_ms))
        input_units.append(float(usage.input_units))
        output_units.append(float(usage.output_units))
        total_units.append(float(usage.total_units))

    def measurement(
        values: list[float],
        *,
        unavailable: int,
    ) -> MeasurementDistribution:
        return MeasurementDistribution(
            attempted=len(cases),
            measured=len(values),
            unavailable=unavailable,
            target_failures=target_failures,
            statistics=_quantiles(
                values,
                suppress_below=MIN_OPERATIONAL_AGGREGATE_SIZE,
            ),
        )

    return RunOperationalDistribution(
        role=role,
        run_id=result.run_id,
        execution_mode=result.execution_mode,
        latency_ms=measurement(latency, unavailable=latency_unavailable),
        input_units=measurement(input_units, unavailable=usage_unavailable),
        output_units=measurement(output_units, unavailable=usage_unavailable),
        total_units=measurement(total_units, unavailable=usage_unavailable),
    )


def _quantiles(
    values: Iterable[float | int],
    *,
    suppress_below: int | None = None,
) -> QuantileSummary:
    ordered = tuple(sorted(float(value) for value in values))
    if not ordered:
        return QuantileSummary(sample_count=0)
    if suppress_below is not None and len(ordered) < suppress_below:
        return QuantileSummary(sample_count=len(ordered), suppressed=True)

    def nearest_rank(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    return QuantileSummary(
        sample_count=len(ordered),
        minimum=ordered[0],
        p50=nearest_rank(0.5),
        p95=nearest_rank(0.95),
        maximum=ordered[-1],
        mean=math.fsum(ordered) / len(ordered),
    )


__all__ = ["build_release_decision_distributions"]
