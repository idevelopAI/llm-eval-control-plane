"""Immutable, content-addressed evaluation suite definitions."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, field_validator, model_validator

from llm_eval_control_plane.domain.artifacts import (
    ArtifactKind,
    ArtifactName,
    ArtifactRef,
    Sha256Digest,
)
from llm_eval_control_plane.domain.canonical import (
    JsonValue,
    canonical_json_bytes,
    sha256_digest,
)
from llm_eval_control_plane.domain.datasets import SliceLabel
from llm_eval_control_plane.domain.evaluation import (
    EvaluationSpec,
    MetricGate,
    MetricName,
)
from llm_eval_control_plane.domain.models import FrozenModel
from llm_eval_control_plane.domain.results import ExecutionMode

ExecutorName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class SuiteCaseOrder(StrEnum):
    """Case ordering modes supported by an evaluation suite version."""

    CASE_ID_ASCENDING = "case_id_ascending"


class SuiteExecutionSettings(FrozenModel):
    """Semantic execution behavior shared by every run of one suite."""

    adapter: ExecutorName
    execution_mode: ExecutionMode
    case_order: SuiteCaseOrder = SuiteCaseOrder.CASE_ID_ASCENDING
    invocations_per_case: Literal[1] = 1
    max_concurrency: Literal[1] = 1


class SuiteEvaluator(FrozenModel):
    """Bind one runtime evaluator selector to exact behavior and metrics."""

    executor_name: ExecutorName
    artifact: ArtifactRef
    metrics: Annotated[tuple[MetricName, ...], Field(min_length=1, max_length=32)]

    @field_validator("metrics")
    @classmethod
    def normalize_metrics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("suite evaluator metrics must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.artifact.kind is not ArtifactKind.EVALUATOR:
            raise ValueError("suite evaluator must reference an evaluator artifact")
        if self.artifact.digest is None:
            raise ValueError("suite evaluator artifact must have a resolved digest")
        return self


def _artifact_record(artifact: ArtifactRef) -> dict[str, JsonValue]:
    return {
        "digest": artifact.digest,
        "kind": artifact.kind.value,
        "name": artifact.name,
        "revision": artifact.revision,
    }


def _evaluator_record(evaluator: SuiteEvaluator) -> dict[str, JsonValue]:
    return {
        "artifact": _artifact_record(evaluator.artifact),
        "executor_name": evaluator.executor_name,
        "metrics": list(evaluator.metrics),
    }


def _execution_record(settings: SuiteExecutionSettings) -> dict[str, JsonValue]:
    return {
        "adapter": settings.adapter,
        "case_order": settings.case_order.value,
        "execution_mode": settings.execution_mode.value,
        "invocations_per_case": settings.invocations_per_case,
        "max_concurrency": settings.max_concurrency,
    }


def _gate_record(gate: MetricGate) -> dict[str, JsonValue]:
    return {
        "allowed_regression": gate.allowed_regression,
        "direction": gate.direction.value,
        "metric": gate.metric,
        "slice": gate.slice,
        "threshold": gate.threshold,
    }


def _evaluator_key(evaluator: SuiteEvaluator) -> tuple[ArtifactKind, str, int]:
    return evaluator.artifact.logical_key


def _gate_key(gate: MetricGate) -> tuple[str, str]:
    return (gate.metric, gate.slice or "")


def suite_content(
    *,
    dataset: ArtifactRef,
    evaluators: tuple[SuiteEvaluator, ...],
    slices: tuple[str, ...],
    execution: SuiteExecutionSettings,
    gates: tuple[MetricGate, ...],
) -> dict[str, JsonValue]:
    """Build the canonical semantic envelope covered by a suite digest."""
    ordered_evaluators = sorted(evaluators, key=_evaluator_key)
    ordered_slices = sorted(slices)
    ordered_gates = sorted(gates, key=_gate_key)
    slice_records: list[JsonValue] = []
    slice_records.extend(ordered_slices)
    return {
        "dataset": _artifact_record(dataset),
        "digest_schema": "evaluation-suite/v1",
        "evaluators": [_evaluator_record(item) for item in ordered_evaluators],
        "execution": _execution_record(execution),
        "gates": [_gate_record(item) for item in ordered_gates],
        "slices": slice_records,
    }


def calculate_suite_digest(
    *,
    dataset: ArtifactRef,
    evaluators: tuple[SuiteEvaluator, ...],
    slices: tuple[str, ...],
    execution: SuiteExecutionSettings,
    gates: tuple[MetricGate, ...],
) -> str:
    """Hash one suite independently from its name, revision, and authoring order."""
    return sha256_digest(
        suite_content(
            dataset=dataset,
            evaluators=evaluators,
            slices=slices,
            execution=execution,
            gates=gates,
        )
    )


class EvaluationSuiteVersion(FrozenModel):
    """One immutable evaluation protocol shared across target revisions."""

    schema_version: Literal["1"] = "1"
    name: ArtifactName
    revision: PositiveInt
    digest: Sha256Digest
    dataset: ArtifactRef
    evaluators: Annotated[
        tuple[SuiteEvaluator, ...], Field(min_length=1, max_length=32)
    ]
    slices: Annotated[tuple[SliceLabel, ...], Field(max_length=128)] = ()
    execution: SuiteExecutionSettings
    gates: Annotated[tuple[MetricGate, ...], Field(min_length=1, max_length=64)]

    @field_validator("evaluators")
    @classmethod
    def normalize_evaluators(
        cls, value: tuple[SuiteEvaluator, ...]
    ) -> tuple[SuiteEvaluator, ...]:
        return tuple(sorted(value, key=_evaluator_key))

    @field_validator("slices")
    @classmethod
    def normalize_slices(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("suite slices must be unique")
        return tuple(sorted(value))

    @field_validator("gates")
    @classmethod
    def normalize_gates(cls, value: tuple[MetricGate, ...]) -> tuple[MetricGate, ...]:
        keys = [_gate_key(gate) for gate in value]
        if len(keys) != len(set(keys)):
            raise ValueError("suite gate metric and slice combinations must be unique")
        return tuple(sorted(value, key=_gate_key))

    @model_validator(mode="after")
    def validate_suite(self) -> Self:
        if self.dataset.kind is not ArtifactKind.DATASET:
            raise ValueError("suite dataset must reference a dataset artifact")
        if self.dataset.digest is None:
            raise ValueError("suite dataset must have a resolved digest")

        executor_names = [item.executor_name for item in self.evaluators]
        if len(executor_names) != len(set(executor_names)):
            raise ValueError("suite evaluator executor names must be unique")

        evaluator_keys = [item.artifact.logical_key for item in self.evaluators]
        if len(evaluator_keys) != len(set(evaluator_keys)):
            raise ValueError("suite evaluator artifacts must be unique")

        metrics = [metric for item in self.evaluators for metric in item.metrics]
        if len(metrics) != len(set(metrics)):
            raise ValueError("suite evaluator metrics must be globally unique")
        if len(metrics) > 32:
            raise ValueError("suite cannot declare more than 32 metrics")

        metric_set = set(metrics)
        slice_set = set(self.slices)
        if any(gate.metric not in metric_set for gate in self.gates):
            raise ValueError("suite gate metric must be emitted by an evaluator")
        if any(
            gate.slice is not None and gate.slice not in slice_set
            for gate in self.gates
        ):
            raise ValueError("suite gate slice must be declared by the suite")

        expected_digest = calculate_suite_digest(
            dataset=self.dataset,
            evaluators=self.evaluators,
            slices=self.slices,
            execution=self.execution,
            gates=self.gates,
        )
        if self.digest != expected_digest:
            raise ValueError("suite digest does not match canonical content")
        return self

    @classmethod
    def create(
        cls,
        *,
        name: str,
        revision: int,
        dataset: ArtifactRef,
        evaluators: tuple[SuiteEvaluator, ...],
        slices: tuple[str, ...],
        execution: SuiteExecutionSettings,
        gates: tuple[MetricGate, ...],
    ) -> EvaluationSuiteVersion:
        """Normalize suite content and calculate its verified digest."""
        return cls(
            name=name,
            revision=revision,
            digest=calculate_suite_digest(
                dataset=dataset,
                evaluators=evaluators,
                slices=slices,
                execution=execution,
                gates=gates,
            ),
            dataset=dataset,
            evaluators=evaluators,
            slices=slices,
            execution=execution,
            gates=gates,
        )

    @property
    def artifact_ref(self) -> ArtifactRef:
        """Return the resolved suite identity for jobs and evidence."""
        return ArtifactRef(
            kind=ArtifactKind.SUITE,
            name=self.name,
            revision=self.revision,
            digest=self.digest,
        )

    @property
    def evaluator_refs(self) -> tuple[ArtifactRef, ...]:
        """Return evaluator identities in canonical suite order."""
        return tuple(item.artifact for item in self.evaluators)

    @property
    def evaluator_names(self) -> tuple[str, ...]:
        """Return runtime evaluator selectors in canonical suite order."""
        return tuple(item.executor_name for item in self.evaluators)

    def to_evaluation_spec(
        self,
        *,
        baseline: ArtifactRef,
        candidate: ArtifactRef,
    ) -> EvaluationSpec:
        """Compile the suite policy for one exact target comparison."""
        return EvaluationSpec(
            name=self.name,
            dataset=self.dataset,
            baseline=baseline,
            candidate=candidate,
            gates=self.gates,
        )

    def canonical_content_bytes(self) -> bytes:
        """Return the exact canonical bytes covered by ``digest``."""
        return canonical_json_bytes(
            suite_content(
                dataset=self.dataset,
                evaluators=self.evaluators,
                slices=self.slices,
                execution=self.execution,
                gates=self.gates,
            )
        )
