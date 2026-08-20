"""Contracts for reproducible evaluation specifications and release gates."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, FiniteFloat, NonNegativeFloat, model_validator

from llm_eval_control_plane.domain.artifacts import (
    ArtifactKind,
    ArtifactName,
    ArtifactRef,
)
from llm_eval_control_plane.domain.models import FrozenModel

MetricName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9._:/-]*$",
    ),
]


class MetricDirection(StrEnum):
    """Whether a metric improves as its value rises or falls."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class MetricGate(FrozenModel):
    """Deterministic acceptance rule for one aggregate metric."""

    metric: MetricName
    direction: MetricDirection
    threshold: FiniteFloat
    allowed_regression: NonNegativeFloat = 0.0


class EvaluationSpec(FrozenModel):
    """Resolved inputs and release criteria for a future evaluation run."""

    schema_version: Literal["1"] = "1"
    name: ArtifactName
    dataset: ArtifactRef
    candidate: ArtifactRef
    baseline: ArtifactRef | None = None
    gates: Annotated[tuple[MetricGate, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Enforce artifact roles and unambiguous gate definitions."""
        if self.dataset.kind is not ArtifactKind.DATASET:
            raise ValueError("dataset must reference a dataset artifact")
        if self.candidate.kind is not ArtifactKind.TARGET:
            raise ValueError("candidate must reference a target artifact")
        if self.baseline is not None:
            if self.baseline.kind is not ArtifactKind.TARGET:
                raise ValueError("baseline must reference a target artifact")
            if self.baseline.logical_key == self.candidate.logical_key:
                raise ValueError("baseline and candidate must be different revisions")

        metric_names = [gate.metric for gate in self.gates]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("gate metric names must be unique")
        return self
