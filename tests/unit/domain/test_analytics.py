from collections.abc import Callable

from pydantic import ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain.analytics import (
    DeltaDistribution,
    MeasurementDistribution,
    QuantileSummary,
    ScoreValueDistribution,
)


def _observed(*, mean: float = 1.0) -> QuantileSummary:
    return QuantileSummary(
        sample_count=1,
        minimum=0.0,
        p50=0.5,
        p95=1.0,
        maximum=1.0,
        mean=mean,
    )


@mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: QuantileSummary(sample_count=0, suppressed=True),
            "empty distributions must omit every statistic",
        ),
        (
            lambda: QuantileSummary(sample_count=0, minimum=0.0),
            "empty distributions must omit every statistic",
        ),
        (
            lambda: QuantileSummary(
                sample_count=1,
                suppressed=True,
                minimum=0.0,
            ),
            "suppressed distributions must omit every statistic",
        ),
        (
            lambda: QuantileSummary(sample_count=1),
            "observed distributions require every statistic",
        ),
        (
            lambda: QuantileSummary(
                sample_count=1,
                minimum=0.0,
                p50=1.0,
                p95=0.5,
                maximum=1.0,
                mean=0.5,
            ),
            "distribution quantiles must be ordered",
        ),
        (
            lambda: _observed(mean=2.0),
            "distribution mean must lie within its range",
        ),
        (
            lambda: ScoreValueDistribution(
                attempted=2,
                scored=1,
                skipped=0,
                errors=0,
                statistics=_observed(),
            ),
            "score distribution counts must equal attempted cases",
        ),
        (
            lambda: ScoreValueDistribution(
                attempted=1,
                scored=1,
                skipped=0,
                errors=0,
                statistics=QuantileSummary(sample_count=0),
            ),
            "score sample count must equal scored cases",
        ),
        (
            lambda: DeltaDistribution(
                attempted=2,
                compared=1,
                incomparable=0,
                statistics=_observed(),
            ),
            "delta distribution counts must equal attempted cases",
        ),
        (
            lambda: DeltaDistribution(
                attempted=1,
                compared=1,
                incomparable=0,
                statistics=QuantileSummary(sample_count=0),
            ),
            "delta sample count must equal comparable cases",
        ),
        (
            lambda: MeasurementDistribution(
                attempted=2,
                measured=1,
                unavailable=0,
                target_failures=0,
                statistics=_observed(),
            ),
            "measurement counts must equal attempted cases",
        ),
        (
            lambda: MeasurementDistribution(
                attempted=1,
                measured=1,
                unavailable=0,
                target_failures=2,
                statistics=_observed(),
            ),
            "target failures cannot exceed attempted cases",
        ),
        (
            lambda: MeasurementDistribution(
                attempted=1,
                measured=1,
                unavailable=0,
                target_failures=0,
                statistics=QuantileSummary(sample_count=0),
            ),
            "measurement sample count must equal measured cases",
        ),
    ],
)
def test_analytical_models_reject_inconsistent_coverage_and_statistics(
    build: Callable[[], object],
    message: str,
) -> None:
    with raises(ValidationError, match=message):
        build()


def test_quantile_small_sample_boundary_is_explicit() -> None:
    small = QuantileSummary(sample_count=19, suppressed=True)
    sufficient = QuantileSummary(
        sample_count=20,
        minimum=0.0,
        p50=0.5,
        p95=1.0,
        maximum=1.0,
        mean=0.5,
    )

    assert small.small_sample is True
    assert sufficient.small_sample is False
