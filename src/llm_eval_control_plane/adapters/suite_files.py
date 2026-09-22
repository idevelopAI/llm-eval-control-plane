"""Bounded local authoring and create-only transport for pinned offline suites."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, field_validator

from llm_eval_control_plane.adapters.scorers import (
    BuiltInEvaluatorKind,
    build_evaluators,
)
from llm_eval_control_plane.domain import (
    DatasetVersion,
    EvaluationSuiteVersion,
    ExecutionMode,
    MetricGate,
    SuiteEvaluator,
    SuiteExecutionSettings,
    canonical_json_bytes,
    parse_json,
)
from llm_eval_control_plane.domain.artifacts import ArtifactName
from llm_eval_control_plane.domain.datasets import SliceLabel
from llm_eval_control_plane.domain.models import FrozenModel

_MAX_DOCUMENT_BYTES = 256 * 1024
_PositiveRevision = Annotated[int, Field(strict=True, gt=0)]


class SuiteDatasetDefinition(FrozenModel):
    name: ArtifactName
    revision: _PositiveRevision


class SuiteGateDefinition(MetricGate):
    threshold: Annotated[FiniteFloat, Field(strict=True)]
    allowed_regression: Annotated[FiniteFloat, Field(strict=True, ge=0)] = 0.0


class SuiteDefinition(FrozenModel):
    """Human-authored selectors; build resolves all behavior before hashing."""

    schema_version: Literal["suite-definition/v1"] = "suite-definition/v1"
    name: ArtifactName
    revision: _PositiveRevision
    dataset: SuiteDatasetDefinition
    evaluators: Annotated[
        tuple[BuiltInEvaluatorKind, ...],
        Field(min_length=1, max_length=len(BuiltInEvaluatorKind)),
    ]
    slices: Annotated[tuple[SliceLabel, ...], Field(max_length=128)] = ()
    gates: Annotated[
        tuple[SuiteGateDefinition, ...], Field(min_length=1, max_length=64)
    ]

    @field_validator("evaluators")
    @classmethod
    def unique_evaluators(
        cls, value: tuple[BuiltInEvaluatorKind, ...]
    ) -> tuple[BuiltInEvaluatorKind, ...]:
        if len(value) != len(set(value)):
            raise ValueError("suite evaluators must be unique")
        return tuple(sorted(value))


def _resolved_evaluators(names: tuple[str, ...]) -> tuple[SuiteEvaluator, ...]:
    kinds = tuple(BuiltInEvaluatorKind(name) for name in names)
    return tuple(
        SuiteEvaluator(
            executor_name=name, artifact=evaluator.ref, metrics=evaluator.metric_names
        )
        for name, evaluator in zip(names, build_evaluators(kinds), strict=True)
    )


def _execution() -> SuiteExecutionSettings:
    return SuiteExecutionSettings(
        adapter="deterministic_fake", execution_mode=ExecutionMode.OFFLINE_MOCK
    )


def resolve_suite(
    definition: SuiteDefinition, dataset: DatasetVersion
) -> EvaluationSuiteVersion:
    """Resolve reviewed local inputs without constructing or invoking a target."""
    if (dataset.name, dataset.revision) != (
        definition.dataset.name,
        definition.dataset.revision,
    ):
        raise ValueError("suite dataset identity does not match")
    suite = EvaluationSuiteVersion.create(
        name=definition.name,
        revision=definition.revision,
        dataset=dataset.artifact_ref,
        evaluators=_resolved_evaluators(
            tuple(kind.value for kind in definition.evaluators)
        ),
        execution=_execution(),
        slices=definition.slices,
        gates=tuple(
            MetricGate.model_validate(gate.model_dump()) for gate in definition.gates
        ),
    )
    validate_local_suite(suite, dataset)
    return suite


def validate_local_suite(
    suite: EvaluationSuiteVersion, dataset: DatasetVersion
) -> None:
    """Check exact dataset, declared slices, and installed executor behavior."""
    if suite.dataset != dataset.artifact_ref:
        raise ValueError("suite dataset content does not match")
    if not set(suite.slices) <= {
        label for case in dataset.cases for label in case.slices
    }:
        raise ValueError("suite slice is absent from the dataset")
    if suite.execution != _execution():
        raise ValueError("suite execution settings are unsupported")
    if suite.evaluators != _resolved_evaluators(suite.evaluator_names):
        raise ValueError("suite evaluator contract has drifted")


def read_suite_definition(path: Path) -> SuiteDefinition:
    return SuiteDefinition.model_validate_json(_read_json(path), strict=True)


def read_suite(path: Path) -> EvaluationSuiteVersion:
    payload = _read_json(path)
    document = parse_json(payload.decode("utf-8"))
    if isinstance(document, dict) and isinstance(
        execution := document.get("execution"), dict
    ):
        # Literal[1] otherwise accepts True and 1.0 even in strict Pydantic mode.
        for field in ("invocations_per_case", "max_concurrency"):
            if field in execution and type(execution[field]) is not int:
                raise ValueError("suite execution counts must be integers")
    return EvaluationSuiteVersion.model_validate_json(payload, strict=True)


def read_suite_scenarios(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    document = parse_json(_read_json(path).decode("utf-8"))
    if not isinstance(document, dict) or len(document) > 1_000:
        raise ValueError("suite scenarios must be a bounded mapping")
    scenarios: dict[str, str] = {}
    for case_id, scenario in document.items():
        if not isinstance(scenario, str):
            raise ValueError("suite scenarios must contain string values")
        scenarios[case_id] = scenario
    return scenarios


def _read_json(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_DOCUMENT_BYTES:
            raise ValueError("suite document must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_DOCUMENT_BYTES + 1)
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise ValueError("suite document exceeds the size limit")
        try:
            canonical_json_bytes(parse_json(payload.decode("utf-8")))
        except RecursionError:
            raise ValueError("suite document nesting exceeds the limit") from None
        # Keep JSON number types intact for strict validation (1.0 is not 1).
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_suite(path: Path, suite: EvaluationSuiteVersion) -> None:
    """Publish complete owner-only canonical bytes, never replace an existing file."""
    payload = canonical_json_bytes(suite.model_dump(mode="json")) + b"\n"
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise ValueError("suite document exceeds the size limit")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".suite-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def suite_summary(suite: EvaluationSuiteVersion) -> dict[str, object]:
    """Show provenance and counts without printing the complete protocol."""
    return {
        "schema_version": "suite-file-summary/v1",
        "suite": suite.artifact_ref.model_dump(mode="json"),
        "dataset": suite.dataset.model_dump(mode="json"),
        "execution_mode": suite.execution.execution_mode.value,
        "evaluator_count": len(suite.evaluators),
        "metric_count": sum(len(item.metrics) for item in suite.evaluators),
        "slice_count": len(suite.slices),
        "gate_count": len(suite.gates),
    }
