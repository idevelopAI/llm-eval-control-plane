"""Dependency-inversion ports owned by the application layer."""

from typing import Protocol

from llm_eval_control_plane.domain import ArtifactRef, EvaluationCase
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import (
    MetricObservation,
    TargetRequest,
    TargetResponse,
)
from llm_eval_control_plane.domain.results import RunResult


class TargetPort(Protocol):
    """Invoke one immutable target revision with untrusted adapter output."""

    @property
    def ref(self) -> ArtifactRef: ...

    async def invoke(self, request: TargetRequest) -> object: ...


class EvaluatorPort(Protocol):
    """Score one validated target response deterministically."""

    @property
    def ref(self) -> ArtifactRef: ...

    @property
    def metric_names(self) -> tuple[MetricName, ...]: ...

    def evaluate(
        self,
        case: EvaluationCase,
        response: TargetResponse,
    ) -> tuple[MetricObservation, ...]: ...


class RunRepository(Protocol):
    """Append and retrieve immutable complete run artifacts."""

    def save(self, result: RunResult) -> None: ...

    def get(self, run_id: str) -> RunResult: ...
