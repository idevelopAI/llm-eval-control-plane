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

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "CanonicalJson",
    "CanonicalJsonError",
    "DatasetVersion",
    "EvaluationCase",
    "EvaluationSpec",
    "MetricDirection",
    "MetricGate",
    "calculate_dataset_digest",
    "canonical_json_bytes",
    "parse_json",
    "sha256_digest",
]
