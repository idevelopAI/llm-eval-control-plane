"""Deterministic built-in evaluators with explicit non-scoring outcomes."""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from enum import StrEnum

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from llm_eval_control_plane.application import EvaluatorPort
from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    ErrorObservation,
    EvaluationCase,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
    TargetOutcome,
    sha256_digest,
)
from llm_eval_control_plane.domain.canonical import JsonValue
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import MetricObservation


class BuiltInEvaluatorKind(StrEnum):
    EXACT_MATCH = "exact_match"
    NORMALIZED_MATCH = "normalized_match"
    JSON_SCHEMA = "json_schema"
    NUMERIC_TOLERANCE = "numeric_tolerance"
    REFUSAL = "refusal"
    LATENCY = "latency"
    USAGE = "usage"


DEFAULT_EVALUATOR_KINDS = tuple(BuiltInEvaluatorKind)


def _evaluator_ref(kind: BuiltInEvaluatorKind) -> ArtifactRef:
    return ArtifactRef(
        kind=ArtifactKind.EVALUATOR,
        name=f"builtin/{kind.value}",
        revision=1,
        digest=sha256_digest(
            {
                "evaluator_schema": "builtin/v1",
                "implementation": kind.value,
            }
        ),
    )


def _scored(
    *,
    metric: str,
    evaluator: ArtifactRef,
    value: float,
    reason_code: str,
) -> MetricObservation:
    return ScoredObservation(
        metric=metric,
        evaluator=evaluator,
        value=value,
        reason_code=reason_code,
    )


def _skipped(
    *, metric: str, evaluator: ArtifactRef, reason_code: str
) -> MetricObservation:
    return SkippedObservation(
        metric=metric,
        evaluator=evaluator,
        reason_code=reason_code,
    )


def _error(
    *,
    metric: str,
    evaluator: ArtifactRef,
    error_code: str,
    message: str,
) -> MetricObservation:
    return ErrorObservation(
        metric=metric,
        evaluator=evaluator,
        error_code=error_code,
        message=message,
    )


class ExactMatchEvaluator:
    """Compare arbitrary JSON values using their canonical representation."""

    _kind = BuiltInEvaluatorKind.EXACT_MATCH
    _metric = "quality.exact_match"

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return (self._metric,)

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        if case.expected is None:
            return (
                _skipped(
                    metric=self._metric,
                    evaluator=self.ref,
                    reason_code="no_expectation",
                ),
            )
        matched = target.response.output == case.expected
        return (
            _scored(
                metric=self._metric,
                evaluator=self.ref,
                value=float(matched),
                reason_code="matched" if matched else "mismatched",
            ),
        )


def normalize_text(value: str) -> str:
    """Apply the documented Unicode and whitespace normalization contract."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


class NormalizedMatchEvaluator:
    """Compare strings after NFKC, case folding, and whitespace collapse."""

    _kind = BuiltInEvaluatorKind.NORMALIZED_MATCH
    _metric = "quality.normalized_match"

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return (self._metric,)

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        if case.expected is None:
            return (
                _skipped(
                    metric=self._metric,
                    evaluator=self.ref,
                    reason_code="no_expectation",
                ),
            )
        expected = case.expected.to_value()
        actual = target.response.output.to_value()
        if not isinstance(expected, str) or not isinstance(actual, str):
            return (
                _error(
                    metric=self._metric,
                    evaluator=self.ref,
                    error_code="non_text_value",
                    message="Normalized matching requires text values",
                ),
            )
        matched = normalize_text(actual) == normalize_text(expected)
        return (
            _scored(
                metric=self._metric,
                evaluator=self.ref,
                value=float(matched),
                reason_code="matched" if matched else "mismatched",
            ),
        )


def _contains_remote_reference(value: JsonValue) -> bool:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            return True
        return any(_contains_remote_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_remote_reference(item) for item in value)
    return False


class JsonSchemaEvaluator:
    """Validate output against a case-local JSON Schema Draft 2020-12 schema."""

    _kind = BuiltInEvaluatorKind.JSON_SCHEMA
    _metric = "quality.json_schema_valid"

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return (self._metric,)

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        if case.expected_schema is None:
            return (
                _skipped(
                    metric=self._metric,
                    evaluator=self.ref,
                    reason_code="no_schema",
                ),
            )
        schema = case.expected_schema.to_value()
        if not isinstance(schema, dict):
            return (
                _error(
                    metric=self._metric,
                    evaluator=self.ref,
                    error_code="invalid_schema",
                    message="Case JSON Schema is invalid",
                ),
            )
        if _contains_remote_reference(schema):
            return (
                _error(
                    metric=self._metric,
                    evaluator=self.ref,
                    error_code="remote_reference",
                    message="Remote JSON Schema references are disabled",
                ),
            )
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            return (
                _error(
                    metric=self._metric,
                    evaluator=self.ref,
                    error_code="invalid_schema",
                    message="Case JSON Schema is invalid",
                ),
            )
        matched = Draft202012Validator(schema).is_valid(
            target.response.output.to_value()
        )
        return (
            _scored(
                metric=self._metric,
                evaluator=self.ref,
                value=float(matched),
                reason_code="valid" if matched else "schema_mismatch",
            ),
        )


class NumericToleranceEvaluator:
    """Compare finite JSON numbers using a case-defined absolute tolerance."""

    _kind = BuiltInEvaluatorKind.NUMERIC_TOLERANCE
    _metric = "quality.numeric_within_tolerance"

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return (self._metric,)

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        if case.numeric_tolerance is None or case.expected is None:
            return (
                _skipped(
                    metric=self._metric,
                    evaluator=self.ref,
                    reason_code="no_tolerance",
                ),
            )
        actual = target.response.output.to_value()
        expected = case.expected.to_value()
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or isinstance(expected, bool)
            or not isinstance(expected, (int, float))
        ):
            return (
                _error(
                    metric=self._metric,
                    evaluator=self.ref,
                    error_code="non_numeric_value",
                    message="Numeric tolerance requires numeric values",
                ),
            )
        difference = abs(Decimal(str(actual)) - Decimal(str(expected)))
        matched = difference <= Decimal(str(case.numeric_tolerance))
        return (
            _scored(
                metric=self._metric,
                evaluator=self.ref,
                value=float(matched),
                reason_code="within_tolerance" if matched else "outside_tolerance",
            ),
        )


class RefusalEvaluator:
    """Compare expected and observed structured refusal state."""

    _kind = BuiltInEvaluatorKind.REFUSAL
    _metric = "safety.refusal_correct"

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return (self._metric,)

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        refused = target.response.outcome is TargetOutcome.REFUSED
        matched = refused is case.expected_refusal
        return (
            _scored(
                metric=self._metric,
                evaluator=self.ref,
                value=float(matched),
                reason_code="matched" if matched else "mismatched",
            ),
        )


class LatencyEvaluator:
    """Capture control-plane measured target duration in milliseconds."""

    _kind = BuiltInEvaluatorKind.LATENCY
    _metric = "performance.latency_ms"

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return (self._metric,)

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        del case
        return (
            _scored(
                metric=self._metric,
                evaluator=self.ref,
                value=target.latency_ms,
                reason_code="observed",
            ),
        )


class UsageEvaluator:
    """Capture required input, output, and total usage counters."""

    _kind = BuiltInEvaluatorKind.USAGE
    _metrics = (
        "usage.input_units",
        "usage.output_units",
        "usage.total_units",
    )

    @property
    def ref(self) -> ArtifactRef:
        return _evaluator_ref(self._kind)

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return self._metrics

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        del case
        usage = target.response.usage
        values = (usage.input_units, usage.output_units, usage.total_units)
        return tuple(
            _scored(
                metric=metric,
                evaluator=self.ref,
                value=float(value),
                reason_code="observed",
            )
            for metric, value in zip(self._metrics, values, strict=True)
        )


_EVALUATOR_TYPES: dict[BuiltInEvaluatorKind, type[EvaluatorPort]] = {
    BuiltInEvaluatorKind.EXACT_MATCH: ExactMatchEvaluator,
    BuiltInEvaluatorKind.NORMALIZED_MATCH: NormalizedMatchEvaluator,
    BuiltInEvaluatorKind.JSON_SCHEMA: JsonSchemaEvaluator,
    BuiltInEvaluatorKind.NUMERIC_TOLERANCE: NumericToleranceEvaluator,
    BuiltInEvaluatorKind.REFUSAL: RefusalEvaluator,
    BuiltInEvaluatorKind.LATENCY: LatencyEvaluator,
    BuiltInEvaluatorKind.USAGE: UsageEvaluator,
}


def build_evaluators(
    kinds: tuple[BuiltInEvaluatorKind, ...] = DEFAULT_EVALUATOR_KINDS,
) -> tuple[EvaluatorPort, ...]:
    """Build a unique, deterministically ordered set of built-in evaluators."""
    if not kinds:
        raise ValueError("at least one evaluator is required")
    if len(kinds) != len(set(kinds)):
        raise ValueError("evaluator kinds must be unique")
    return tuple(_EVALUATOR_TYPES[kind]() for kind in kinds)
