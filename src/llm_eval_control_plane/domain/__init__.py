"""Immutable contracts shared by control-plane entry points and adapters."""

from llm_eval_control_plane.domain.artifacts import ArtifactKind, ArtifactRef
from llm_eval_control_plane.domain.canonical import (
    CanonicalJson,
    CanonicalJsonError,
    canonical_json_bytes,
    parse_json,
    sha256_digest,
)
from llm_eval_control_plane.domain.datasets import (
    DatasetVersion,
    EvaluationCase,
    calculate_dataset_digest,
)
from llm_eval_control_plane.domain.evaluation import (
    EvaluationSpec,
    MetricDirection,
    MetricGate,
)
from llm_eval_control_plane.domain.execution import (
    ErrorObservation,
    ExecutionFailure,
    FailureCode,
    FailureStage,
    ObservationStatus,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
    TokenUsage,
)
from llm_eval_control_plane.domain.results import (
    CaseResult,
    CaseResultStatus,
    MetricSummary,
    RunResult,
    RunStatus,
    calculate_run_digest,
)

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "CanonicalJson",
    "CanonicalJsonError",
    "CaseResult",
    "CaseResultStatus",
    "DatasetVersion",
    "ErrorObservation",
    "EvaluationCase",
    "EvaluationSpec",
    "ExecutionFailure",
    "FailureCode",
    "FailureStage",
    "MetricDirection",
    "MetricGate",
    "MetricSummary",
    "ObservationStatus",
    "RunResult",
    "RunStatus",
    "ScoredObservation",
    "SkippedObservation",
    "TargetObservation",
    "TargetOutcome",
    "TargetRequest",
    "TargetResponse",
    "TokenUsage",
    "calculate_dataset_digest",
    "calculate_run_digest",
    "canonical_json_bytes",
    "parse_json",
    "sha256_digest",
]
