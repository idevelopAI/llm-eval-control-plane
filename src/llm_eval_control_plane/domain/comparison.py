"""Immutable candidate/baseline comparisons and release decisions."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, FiniteFloat, NonNegativeInt, PositiveInt, model_validator

from llm_eval_control_plane.domain.artifacts import (
    ArtifactKind,
    ArtifactName,
    ArtifactRef,
    Sha256Digest,
)
from llm_eval_control_plane.domain.canonical import sha256_digest
from llm_eval_control_plane.domain.datasets import CaseId, SliceLabel
from llm_eval_control_plane.domain.evaluation import MetricDirection, MetricName
from llm_eval_control_plane.domain.execution import RunId
from llm_eval_control_plane.domain.models import FrozenModel
from llm_eval_control_plane.domain.results import ExecutionMode


class ComparisonValueStatus(StrEnum):
    """Whether one side produced comparable metric evidence."""

    SCORED = "scored"
    SKIPPED = "skipped"
    ERROR = "error"


class ComparisonValue(FrozenModel):
    """One candidate or baseline case-level metric value."""

    status: ComparisonValueStatus
    value: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if (self.status is ComparisonValueStatus.SCORED) != (self.value is not None):
            raise ValueError("only scored comparison values contain a number")
        return self


class CaseChange(StrEnum):
    """Threshold-relative change for one case and gate."""

    NEWLY_PASSING = "newly_passing"
    NEWLY_FAILING = "newly_failing"
    UNCHANGED_PASSING = "unchanged_passing"
    UNCHANGED_FAILING = "unchanged_failing"
    INCOMPARABLE = "incomparable"


class GateCaseComparison(FrozenModel):
    """Case-level evidence interpreted using one release gate."""

    metric: MetricName
    slice: SliceLabel | None = None
    case_id: CaseId
    slices: tuple[SliceLabel, ...] = ()
    baseline: ComparisonValue
    candidate: ComparisonValue
    delta: FiniteFloat | None = None
    baseline_passed: bool | None = None
    candidate_passed: bool | None = None
    change: CaseChange

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        comparable = (
            self.baseline.status is ComparisonValueStatus.SCORED
            and self.candidate.status is ComparisonValueStatus.SCORED
        )
        if comparable != (self.delta is not None):
            raise ValueError("case delta exists exactly when both values are scored")
        pass_states_exist = (
            self.baseline_passed is not None and self.candidate_passed is not None
        )
        if comparable != pass_states_exist:
            raise ValueError("case pass states exist exactly for comparable values")
        expected_change = CaseChange.INCOMPARABLE
        if comparable:
            # The pass-state equivalence check above narrows both values.
            assert self.baseline_passed is not None  # noqa: S101
            assert self.candidate_passed is not None  # noqa: S101
            expected_change = {
                (False, False): CaseChange.UNCHANGED_FAILING,
                (False, True): CaseChange.NEWLY_PASSING,
                (True, False): CaseChange.NEWLY_FAILING,
                (True, True): CaseChange.UNCHANGED_PASSING,
            }[(self.baseline_passed, self.candidate_passed)]
        if self.change is not expected_change:
            raise ValueError("case change does not match its pass states")
        if tuple(sorted(self.slices)) != self.slices:
            raise ValueError("case comparison slices must be canonically ordered")
        return self


class MetricAggregate(FrozenModel):
    """Coverage-aware metric aggregate for one run and slice."""

    attempted: PositiveInt
    scored: NonNegativeInt
    skipped: NonNegativeInt
    errors: NonNegativeInt
    mean: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.attempted != self.scored + self.skipped + self.errors:
            raise ValueError("aggregate outcome counts must equal attempted cases")
        if (self.scored == 0) != (self.mean is None):
            raise ValueError("aggregate mean exists exactly when values were scored")
        return self


class AggregateComparison(FrozenModel):
    """Candidate/baseline aggregate and candidate-minus-baseline delta."""

    metric: MetricName
    slice: SliceLabel | None = None
    evaluator: ArtifactRef
    baseline: MetricAggregate
    candidate: MetricAggregate
    delta: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if self.evaluator.kind is not ArtifactKind.EVALUATOR:
            raise ValueError("aggregate evaluator must reference an evaluator")
        if self.evaluator.digest is None:
            raise ValueError("aggregate evaluator must have a resolved digest")
        means_exist = self.baseline.mean is not None and self.candidate.mean is not None
        if means_exist != (self.delta is not None):
            raise ValueError("aggregate delta exists exactly when both means exist")
        if self.baseline.attempted != self.candidate.attempted:
            raise ValueError("aggregate sides must attempt the same number of cases")
        return self


class GateFailureCode(StrEnum):
    """Stable reason a release gate did not pass."""

    COVERAGE = "coverage"
    THRESHOLD = "threshold"
    REGRESSION = "regression"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class GateResult(FrozenModel):
    """Deterministic decision evidence for one configured gate."""

    metric: MetricName
    slice: SliceLabel | None = None
    direction: MetricDirection
    threshold: FiniteFloat
    allowed_regression: Annotated[FiniteFloat, Field(ge=0)] = 0.0
    aggregate: AggregateComparison
    coverage_passed: bool
    threshold_passed: bool
    regression_passed: bool
    status: GateStatus
    failure_codes: tuple[GateFailureCode, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (self.metric, self.slice) != (
            self.aggregate.metric,
            self.aggregate.slice,
        ):
            raise ValueError("gate result must reference its aggregate")
        failures: list[GateFailureCode] = []
        if not self.coverage_passed:
            failures.append(GateFailureCode.COVERAGE)
        if not self.threshold_passed:
            failures.append(GateFailureCode.THRESHOLD)
        if not self.regression_passed:
            failures.append(GateFailureCode.REGRESSION)
        expected_failures = tuple(failures)
        if self.failure_codes != expected_failures:
            raise ValueError("gate failure codes do not match gate checks")
        expected_status = (
            GateStatus.PASSED if not expected_failures else GateStatus.FAILED
        )
        if self.status is not expected_status:
            raise ValueError("gate status does not match gate checks")
        return self


class ReleaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


def calculate_decision_digest(
    *,
    spec_name: str,
    dataset: ArtifactRef,
    baseline: ArtifactRef,
    candidate: ArtifactRef,
    baseline_result_digest: str,
    candidate_result_digest: str,
    aggregates: tuple[AggregateComparison, ...],
    gates: tuple[GateResult, ...],
    cases: tuple[GateCaseComparison, ...],
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
) -> str:
    """Hash complete stable release evidence, excluding run identifiers."""
    record: dict[str, object] = {
        "aggregates": [item.model_dump(mode="json") for item in aggregates],
        "baseline": baseline.model_dump(mode="json"),
        "baseline_result_digest": baseline_result_digest,
        "candidate": candidate.model_dump(mode="json"),
        "candidate_result_digest": candidate_result_digest,
        "cases": [item.model_dump(mode="json") for item in cases],
        "dataset": dataset.model_dump(mode="json"),
        "decision_schema": "release-decision/v1",
        "gates": [item.model_dump(mode="json") for item in gates],
        "spec_name": spec_name,
    }
    if execution_mode is not ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE:
        record["decision_schema"] = "release-decision/v2"
        record["execution_mode"] = execution_mode.value
    return sha256_digest(record)


class ReleaseDecision(FrozenModel):
    """Complete candidate/baseline evidence and final release status."""

    schema_version: Literal["1"] = "1"
    spec_name: ArtifactName
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE
    dataset: ArtifactRef
    baseline: ArtifactRef
    candidate: ArtifactRef
    baseline_run_id: RunId
    candidate_run_id: RunId
    baseline_result_digest: Sha256Digest
    candidate_result_digest: Sha256Digest
    aggregates: Annotated[tuple[AggregateComparison, ...], Field(min_length=1)]
    gates: Annotated[tuple[GateResult, ...], Field(min_length=1)]
    cases: Annotated[tuple[GateCaseComparison, ...], Field(min_length=1)]
    status: ReleaseStatus
    decision_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.dataset.kind is not ArtifactKind.DATASET or self.dataset.digest is None:
            raise ValueError("decision dataset must be a resolved dataset")
        if any(
            target.kind is not ArtifactKind.TARGET or target.digest is None
            for target in (self.baseline, self.candidate)
        ):
            raise ValueError("decision targets must be resolved target artifacts")
        aggregate_keys = [(item.metric, item.slice or "") for item in self.aggregates]
        gate_keys = [(item.metric, item.slice or "") for item in self.gates]
        case_keys = [
            (item.metric, item.slice or "", item.case_id) for item in self.cases
        ]
        if aggregate_keys != sorted(aggregate_keys):
            raise ValueError("decision aggregates must be canonically ordered")
        if gate_keys != sorted(gate_keys):
            raise ValueError("decision gates must be canonically ordered")
        if case_keys != sorted(case_keys):
            raise ValueError("decision cases must be canonically ordered")
        if len(gate_keys) != len(set(gate_keys)):
            raise ValueError("decision gates must be unique")
        expected_status = (
            ReleaseStatus.PASSED
            if all(gate.status is GateStatus.PASSED for gate in self.gates)
            else ReleaseStatus.FAILED
        )
        if self.status is not expected_status:
            raise ValueError("release status does not match gate results")
        expected_digest = calculate_decision_digest(
            spec_name=self.spec_name,
            dataset=self.dataset,
            baseline=self.baseline,
            candidate=self.candidate,
            baseline_result_digest=self.baseline_result_digest,
            candidate_result_digest=self.candidate_result_digest,
            aggregates=self.aggregates,
            gates=self.gates,
            cases=self.cases,
            execution_mode=self.execution_mode,
        )
        if self.decision_digest != expected_digest:
            raise ValueError("decision digest does not match canonical evidence")
        return self

    @classmethod
    def create(
        cls,
        *,
        spec_name: str,
        dataset: ArtifactRef,
        baseline: ArtifactRef,
        candidate: ArtifactRef,
        baseline_run_id: str,
        candidate_run_id: str,
        baseline_result_digest: str,
        candidate_result_digest: str,
        aggregates: tuple[AggregateComparison, ...],
        gates: tuple[GateResult, ...],
        cases: tuple[GateCaseComparison, ...],
        execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
    ) -> ReleaseDecision:
        ordered_aggregates = tuple(
            sorted(aggregates, key=lambda item: (item.metric, item.slice or ""))
        )
        ordered_gates = tuple(
            sorted(gates, key=lambda item: (item.metric, item.slice or ""))
        )
        ordered_cases = tuple(
            sorted(
                cases, key=lambda item: (item.metric, item.slice or "", item.case_id)
            )
        )
        status = (
            ReleaseStatus.PASSED
            if all(gate.status is GateStatus.PASSED for gate in ordered_gates)
            else ReleaseStatus.FAILED
        )
        digest = calculate_decision_digest(
            spec_name=spec_name,
            dataset=dataset,
            baseline=baseline,
            candidate=candidate,
            baseline_result_digest=baseline_result_digest,
            candidate_result_digest=candidate_result_digest,
            aggregates=ordered_aggregates,
            gates=ordered_gates,
            cases=ordered_cases,
            execution_mode=execution_mode,
        )
        return cls(
            spec_name=spec_name,
            execution_mode=execution_mode,
            dataset=dataset,
            baseline=baseline,
            candidate=candidate,
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            baseline_result_digest=baseline_result_digest,
            candidate_result_digest=candidate_result_digest,
            aggregates=ordered_aggregates,
            gates=ordered_gates,
            cases=ordered_cases,
            status=status,
            decision_digest=digest,
        )
