"""Bounded, content-free analytical read models for control-plane dashboards."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import FiniteFloat, NonNegativeInt, model_validator

from llm_eval_control_plane.domain.datasets import SliceLabel
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import RunId
from llm_eval_control_plane.domain.models import FrozenModel
from llm_eval_control_plane.domain.results import ExecutionMode

MIN_OPERATIONAL_AGGREGATE_SIZE = 20


class QuantileSummary(FrozenModel):
    """Fixed summary statistics without retaining or exposing raw samples."""

    sample_count: NonNegativeInt
    suppressed: bool = False
    minimum: FiniteFloat | None = None
    p50: FiniteFloat | None = None
    p95: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    mean: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        values = (self.minimum, self.p50, self.p95, self.maximum, self.mean)
        if self.sample_count == 0:
            if self.suppressed or not all(value is None for value in values):
                raise ValueError("empty distributions must omit every statistic")
        elif self.suppressed:
            if not all(value is None for value in values):
                raise ValueError("suppressed distributions must omit every statistic")
        else:
            if any(value is None for value in values):
                raise ValueError("observed distributions require every statistic")
            assert self.minimum is not None  # noqa: S101
            assert self.p50 is not None  # noqa: S101
            assert self.p95 is not None  # noqa: S101
            assert self.maximum is not None  # noqa: S101
            assert self.mean is not None  # noqa: S101
            if not self.minimum <= self.p50 <= self.p95 <= self.maximum:
                raise ValueError("distribution quantiles must be ordered")
            if not self.minimum <= self.mean <= self.maximum:
                raise ValueError("distribution mean must lie within its range")
        return self

    @property
    def small_sample(self) -> bool:
        """Flag descriptive summaries that are too small for stable inference."""
        return self.sample_count < MIN_OPERATIONAL_AGGREGATE_SIZE


class ScoreValueDistribution(FrozenModel):
    """Coverage and fixed quantiles for one side of a gate comparison."""

    attempted: NonNegativeInt
    scored: NonNegativeInt
    skipped: NonNegativeInt
    errors: NonNegativeInt
    statistics: QuantileSummary

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.attempted != self.scored + self.skipped + self.errors:
            raise ValueError("score distribution counts must equal attempted cases")
        if self.statistics.sample_count != self.scored:
            raise ValueError("score sample count must equal scored cases")
        return self


class DeltaDistribution(FrozenModel):
    """Fixed candidate-minus-baseline distribution for comparable cases."""

    attempted: NonNegativeInt
    compared: NonNegativeInt
    incomparable: NonNegativeInt
    statistics: QuantileSummary

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.attempted != self.compared + self.incomparable:
            raise ValueError("delta distribution counts must equal attempted cases")
        if self.statistics.sample_count != self.compared:
            raise ValueError("delta sample count must equal comparable cases")
        return self


class ScoreDistribution(FrozenModel):
    """One configured gate's baseline, candidate, and delta distributions."""

    metric: MetricName
    gate_slice: SliceLabel | None = None
    baseline: ScoreValueDistribution
    candidate: ScoreValueDistribution
    delta: DeltaDistribution


class MeasurementDistribution(FrozenModel):
    """Coverage and fixed quantiles for latency or usage measurements."""

    attempted: NonNegativeInt
    measured: NonNegativeInt
    unavailable: NonNegativeInt
    target_failures: NonNegativeInt
    statistics: QuantileSummary

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.attempted != self.measured + self.unavailable:
            raise ValueError("measurement counts must equal attempted cases")
        if self.target_failures > self.attempted:
            raise ValueError("target failures cannot exceed attempted cases")
        if self.statistics.sample_count != self.measured:
            raise ValueError("measurement sample count must equal measured cases")
        return self


class RunOperationalDistribution(FrozenModel):
    """Content-free operational measurements for one pinned comparison run."""

    role: Literal["baseline", "candidate"]
    run_id: RunId
    execution_mode: ExecutionMode
    latency_ms: MeasurementDistribution
    input_units: MeasurementDistribution
    output_units: MeasurementDistribution
    total_units: MeasurementDistribution


class ReleaseDecisionDistributions(FrozenModel):
    """Fixed-size analytical projection derived from immutable run evidence."""

    decision_id: str
    score: ScoreDistribution
    baseline: RunOperationalDistribution
    candidate: RunOperationalDistribution


__all__ = [
    "MIN_OPERATIONAL_AGGREGATE_SIZE",
    "DeltaDistribution",
    "MeasurementDistribution",
    "QuantileSummary",
    "ReleaseDecisionDistributions",
    "RunOperationalDistribution",
    "ScoreDistribution",
    "ScoreValueDistribution",
]
