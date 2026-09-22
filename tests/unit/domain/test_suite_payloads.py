from pydantic import TypeAdapter
from pytest import raises

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    EvaluationSuiteVersion,
    ExecutionMode,
    MetricDirection,
    MetricGate,
    SuiteEvaluator,
    SuiteExecutionSettings,
    sha256_digest,
)
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    ExecutionContract,
    JobPayload,
    RunJobPayload,
)


def _ref(kind: ArtifactKind, name: str) -> ArtifactRef:
    return ArtifactRef(kind=kind, name=name, revision=1, digest=sha256_digest(name))


def _suite(*, threshold: float = 1.0) -> EvaluationSuiteVersion:
    return EvaluationSuiteVersion.create(
        name="release/core",
        revision=1,
        dataset=_ref(ArtifactKind.DATASET, "cases"),
        evaluators=(
            SuiteEvaluator(
                executor_name="exact_match",
                artifact=_ref(ArtifactKind.EVALUATOR, "builtin/exact_match"),
                metrics=("quality.exact_match",),
            ),
        ),
        execution=SuiteExecutionSettings(
            adapter="deterministic_fake", execution_mode=ExecutionMode.OFFLINE_MOCK
        ),
        slices=(),
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=threshold,
            ),
        ),
    )


def _run_payload(*, suite: EvaluationSuiteVersion | None) -> RunJobPayload:
    settings = suite or _suite()
    target = _ref(ArtifactKind.TARGET, "fake/candidate")
    return RunJobPayload(
        schema_version="run-job/v2" if suite is not None else "run-job/v1",
        suite=suite,
        dataset=settings.dataset,
        target_name=target.name,
        target_revision=target.revision,
        adapter=settings.execution.adapter,
        evaluator_names=settings.evaluator_names,
        execution_contract=ExecutionContract(
            adapter=settings.execution.adapter,
            evaluator_names=settings.evaluator_names,
            target=target,
            evaluators=settings.evaluator_refs,
            execution_mode=settings.execution.execution_mode,
        ),
    )


def _comparison_payload(
    *, suite: EvaluationSuiteVersion | None
) -> ComparisonJobPayload:
    settings = suite or _suite()
    return ComparisonJobPayload(
        schema_version="comparison-job/v2"
        if suite is not None
        else "comparison-job/v1",
        suite=suite,
        dataset=settings.dataset,
        baseline_run_id="baseline",
        baseline_result_digest=sha256_digest("baseline result"),
        candidate_run_id="candidate",
        candidate_result_digest=sha256_digest("candidate result"),
        spec=settings.to_evaluation_spec(
            baseline=_ref(ArtifactKind.TARGET, "fake/baseline"),
            candidate=_ref(ArtifactKind.TARGET, "fake/candidate"),
        ),
    )


def test_legacy_payloads_omit_suite_and_keep_original_digest_projection() -> None:
    for payload in (_run_payload(suite=None), _comparison_payload(suite=None)):
        document = payload.model_dump(mode="json")
        assert "suite" not in document
        assert "suite" not in payload.model_dump(mode="python")
        assert payload.payload_digest == sha256_digest(document)
        restored: JobPayload = TypeAdapter(JobPayload).validate_json(
            payload.model_dump_json()
        )
        assert restored == payload
        assert restored.payload_digest == payload.payload_digest


def test_suite_payloads_round_trip_full_snapshot_and_pin_policy_digest() -> None:
    suite = _suite()
    for factory in (_run_payload, _comparison_payload):
        payload = factory(suite=suite)
        assert payload.suite == suite
        assert payload.model_dump(mode="json")["suite"] == suite.model_dump(mode="json")
        restored: JobPayload = TypeAdapter(JobPayload).validate_json(
            payload.model_dump_json()
        )
        assert restored == payload
        assert restored.payload_digest == payload.payload_digest
        assert (
            factory(suite=_suite(threshold=0.9)).payload_digest
            != payload.payload_digest
        )
        assert suite.digest not in repr(payload)


def test_payload_versions_reject_missing_and_unversioned_suite_snapshots() -> None:
    suite = _suite()
    for payload in (_run_payload(suite=suite), _comparison_payload(suite=suite)):
        document = payload.model_dump(mode="json")
        with raises(ValueError, match="snapshots require"):
            type(payload).model_validate({**document, "suite": None})
        with raises(ValueError, match="snapshots require"):
            type(payload).model_validate(
                {
                    **document,
                    "schema_version": payload.schema_version.replace("v2", "v1"),
                }
            )


def test_run_snapshot_rejects_dataset_or_execution_contract_drift() -> None:
    payload = _run_payload(suite=_suite())
    document = payload.model_dump(mode="python")
    with raises(ValueError, match="dataset does not match suite"):
        RunJobPayload.model_validate(
            {**document, "dataset": _ref(ArtifactKind.DATASET, "other-cases")}
        )
    wrong = payload.execution_contract.model_copy(
        update={"execution_mode": ExecutionMode.LIVE}
    )
    with raises(ValueError, match="contract does not match suite"):
        RunJobPayload.model_validate({**document, "execution_contract": wrong})


def test_comparison_snapshot_rejects_replacement_policy() -> None:
    payload = _comparison_payload(suite=_suite())
    changed = _comparison_payload(suite=_suite(threshold=0.5)).spec
    with raises(ValueError, match="policy does not match suite"):
        ComparisonJobPayload.model_validate(
            {**payload.model_dump(mode="python"), "spec": changed}
        )
