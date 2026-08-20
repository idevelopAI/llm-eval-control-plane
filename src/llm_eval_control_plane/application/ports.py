"""Dependency-inversion ports owned by the application layer."""

from typing import Protocol

from llm_eval_control_plane.domain import ArtifactRef, EvaluationCase, FailureCode
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import (
    MetricObservation,
    TargetObservation,
    TargetRequest,
)
from llm_eval_control_plane.domain.results import RunResult


class TargetPort(Protocol):
    """Invoke one immutable target revision with untrusted adapter output."""

    @property
    def ref(self) -> ArtifactRef: ...

    async def invoke(self, request: TargetRequest) -> object: ...


class TargetInvocationError(RuntimeError):
    """Typed, content-safe target failure raised by infrastructure adapters."""

    def __init__(self, *, code: FailureCode, retryable: bool) -> None:
        super().__init__("Target invocation failed")
        if code not in {
            FailureCode.TARGET_AUTHENTICATION,
            FailureCode.TARGET_PROTOCOL_ERROR,
            FailureCode.TARGET_RATE_LIMITED,
            FailureCode.TARGET_REJECTED,
            FailureCode.TARGET_TIMEOUT,
            FailureCode.TARGET_UNAVAILABLE,
        }:
            raise ValueError("invocation failure requires a target transport code")
        self.code = code
        self.retryable = retryable


class EvaluatorPort(Protocol):
    """Score one validated target response deterministically."""

    @property
    def ref(self) -> ArtifactRef: ...

    @property
    def metric_names(self) -> tuple[MetricName, ...]: ...

    def evaluate(
        self,
        case: EvaluationCase,
        target: TargetObservation,
    ) -> tuple[MetricObservation, ...]: ...


class RunRepository(Protocol):
    """Append and retrieve immutable complete run artifacts."""

    def save(self, result: RunResult) -> None: ...

    def get(self, run_id: str) -> RunResult: ...
