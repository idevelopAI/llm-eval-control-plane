from pydantic import TypeAdapter, ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    CaseResult,
    CaseResultStatus,
    ExecutionFailure,
    ExecutionMode,
    FailureCode,
    FailureStage,
    MetricSummary,
    RunResult,
    RunStatus,
    ScoredObservation,
    TargetObservation,
    TargetResponse,
    TokenUsage,
)
from llm_eval_control_plane.domain.canonical import sha256_digest


def ref(kind: ArtifactKind, name: str) -> ArtifactRef:
    digest_character = {
        ArtifactKind.DATASET: "d",
        ArtifactKind.TARGET: "a",
        ArtifactKind.EVALUATOR: "e",
        ArtifactKind.SUITE: "f",
    }[kind]
    return ArtifactRef(
        kind=kind,
        name=name,
        revision=1,
        digest="sha256:" + digest_character * 64,
    )


DATASET = ref(ArtifactKind.DATASET, "dataset")
TARGET = ref(ArtifactKind.TARGET, "target")
EVALUATOR = ref(ArtifactKind.EVALUATOR, "evaluator")
SECOND_EVALUATOR = ref(ArtifactKind.EVALUATOR, "second-evaluator")
SUITE = ref(ArtifactKind.SUITE, "release-suite")


def target_observation() -> TargetObservation:
    return TargetObservation(
        response=TargetResponse(
            output=CanonicalJson.from_value("answer"),
            usage=TokenUsage(input_units=2, output_units=1),
        ),
        latency_ms=5.0,
    )


def completed_case(case_id: str = "case-1") -> CaseResult:
    return CaseResult(
        case_id=case_id,
        status=CaseResultStatus.COMPLETED,
        target=target_observation(),
        observations=(
            ScoredObservation(
                metric="exact_match",
                evaluator=EVALUATOR,
                value=1.0,
                reason_code="matched",
            ),
        ),
    )


def summary(*, errors: int = 0, scored: int = 1) -> MetricSummary:
    return MetricSummary(
        metric="exact_match",
        evaluator=EVALUATOR,
        attempted=1,
        scored=scored,
        skipped=0,
        errors=errors,
        mean=1.0 if scored else None,
    )


def run_result(
    *,
    suite: ArtifactRef | None = None,
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
) -> RunResult:
    return RunResult.create(
        run_id="run",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=(completed_case(),),
        metrics=(summary(),),
        execution_mode=execution_mode,
        suite=suite,
    )


def test_case_result_validates_completed_and_target_failed_states() -> None:
    assert completed_case().status is CaseResultStatus.COMPLETED

    failed = CaseResult(
        case_id="case-2",
        status=CaseResultStatus.TARGET_FAILED,
        target_failure=ExecutionFailure(
            stage=FailureStage.TARGET,
            code=FailureCode.INVALID_TARGET_OUTPUT,
            message="Target result failed contract validation",
        ),
    )
    assert failed.target is None

    with raises(ValidationError, match="require only a target failure"):
        CaseResult(case_id="invalid", status=CaseResultStatus.TARGET_FAILED)


def test_case_result_rejects_duplicate_observations() -> None:
    observation = completed_case().observations[0]
    with raises(ValidationError, match="unique evaluator metrics"):
        CaseResult(
            case_id="duplicate",
            status=CaseResultStatus.COMPLETED,
            target=target_observation(),
            observations=(observation, observation),
        )


def test_case_result_requires_canonical_nested_ordering() -> None:
    first = completed_case().observations[0]
    second = ScoredObservation(
        metric="second_metric",
        evaluator=SECOND_EVALUATOR,
        value=1.0,
        reason_code="observed",
    )
    with raises(ValidationError, match="observations must be canonically ordered"):
        CaseResult(
            case_id="unordered",
            status=CaseResultStatus.COMPLETED,
            target=target_observation(),
            observations=(second, first),
        )

    failures = tuple(
        ExecutionFailure(
            stage=FailureStage.EVALUATOR,
            code=FailureCode.EVALUATOR_EXCEPTION,
            message="Evaluator raised an exception",
            evaluator=evaluator,
        )
        for evaluator in (SECOND_EVALUATOR, EVALUATOR)
    )
    with raises(ValidationError, match="failures must be canonically ordered"):
        CaseResult(
            case_id="unordered",
            status=CaseResultStatus.COMPLETED_WITH_ERRORS,
            target=target_observation(),
            evaluator_failures=failures,
        )


def test_metric_summary_counts_every_attempt_and_requires_mean() -> None:
    assert summary().mean == 1.0

    with raises(ValidationError, match="must equal attempted"):
        MetricSummary(
            metric="exact_match",
            evaluator=EVALUATOR,
            attempted=2,
            scored=1,
            skipped=0,
            errors=0,
            mean=1.0,
        )
    with raises(ValidationError, match="exists exactly"):
        MetricSummary(
            metric="exact_match",
            evaluator=EVALUATOR,
            attempted=1,
            scored=0,
            skipped=1,
            errors=0,
            mean=0.0,
        )


def test_run_result_is_sorted_content_addressed_and_run_id_independent() -> None:
    cases = (completed_case("case-b"), completed_case("case-a"))
    first = RunResult.create(
        run_id="run-one",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=cases,
        metrics=(
            MetricSummary(
                metric="exact_match",
                evaluator=EVALUATOR,
                attempted=2,
                scored=2,
                skipped=0,
                errors=0,
                mean=1.0,
            ),
        ),
    )
    second = first.model_copy(update={"run_id": "run-two"})

    assert first.status is RunStatus.COMPLETED
    assert [case.case_id for case in first.cases] == ["case-a", "case-b"]
    assert first.result_digest == second.result_digest


def test_execution_mode_is_digest_covered_for_provider_runs() -> None:
    live = RunResult.create(
        run_id="live",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=(completed_case(),),
        metrics=(summary(),),
        execution_mode=ExecutionMode.LIVE,
    )
    mock = RunResult.create(
        run_id="mock",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=(completed_case(),),
        metrics=(summary(),),
        execution_mode=ExecutionMode.OFFLINE_MOCK,
    )

    assert live.execution_mode is ExecutionMode.LIVE
    assert live.result_digest != mock.result_digest

    payload = live.model_dump()
    payload["execution_mode"] = ExecutionMode.OFFLINE_MOCK
    with raises(ValidationError, match="digest does not match"):
        RunResult.model_validate(payload)


def test_run_result_rejects_tampering_and_inconsistent_status() -> None:
    run = RunResult.create(
        run_id="run",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=(completed_case(),),
        metrics=(summary(),),
    )
    payload = run.model_dump()
    payload["result_digest"] = "sha256:" + "0" * 64
    with raises(ValidationError, match="digest does not match"):
        RunResult.model_validate(payload)

    payload = run.model_dump()
    payload["status"] = RunStatus.COMPLETED_WITH_FAILURES
    with raises(ValidationError, match="status does not match"):
        RunResult.model_validate(payload)


def test_run_result_requires_canonical_top_level_ordering() -> None:
    run = RunResult.create(
        run_id="run",
        dataset=DATASET,
        target=TARGET,
        evaluators=(SECOND_EVALUATOR, EVALUATOR),
        cases=(completed_case("case-b"), completed_case("case-a")),
        metrics=(
            summary(),
            MetricSummary(
                metric="second_metric",
                evaluator=SECOND_EVALUATOR,
                attempted=1,
                scored=1,
                skipped=0,
                errors=0,
                mean=1.0,
            ),
        ),
    )

    payload = run.model_dump()
    payload["evaluators"] = tuple(reversed(payload["evaluators"]))
    with raises(ValidationError, match="references must be canonically ordered"):
        RunResult.model_validate(payload)

    payload = run.model_dump()
    payload["cases"] = tuple(reversed(payload["cases"]))
    with raises(ValidationError, match="case results must be canonically ordered"):
        RunResult.model_validate(payload)

    payload = run.model_dump()
    payload["metrics"] = tuple(reversed(payload["metrics"]))
    with raises(ValidationError, match="summaries must be canonically ordered"):
        RunResult.model_validate(payload)


@mark.parametrize(
    ("execution_mode", "result_digest", "document_digest"),
    (
        (
            ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
            "sha256:6656882276c0f5b4c66df5c71ebeb756a50a2de96d94eab57018f80b29739c68",
            "sha256:c4f8f0df0c41bae93b8d7d46fef2d176759966dbd194a0abc5e4d1b61a8eb3f3",
        ),
        (
            ExecutionMode.OFFLINE_MOCK,
            "sha256:7ee875690391f3cac99ee8f3bb641b08571c1f4315507c9cdf32e2fa4249e678",
            "sha256:8ca93a2544007bc9092f5985c2e76dc1dde5cf9ba676aaec9866fb46b1c8c33a",
        ),
        (
            ExecutionMode.LIVE,
            "sha256:5ee14bd258d2af1e5af4f0126d7fe4fcdb48c825920a9b70e533abce0c692a2e",
            "sha256:c311f454157cad11ba5e56134d4e3328bf5e9a7b7011d9da54b079a3fdc8feee",
        ),
    ),
)
def test_unpinned_runs_preserve_historical_digest_and_document_bytes(
    execution_mode: ExecutionMode, result_digest: str, document_digest: str
) -> None:
    # Golden fingerprints were captured from the pre-suite v1/v2 implementation.
    run = run_result(execution_mode=execution_mode)
    payload = run.model_dump(mode="json")
    assert run.result_digest == result_digest
    assert sha256_digest(payload) == document_digest
    assert "suite" not in run.model_dump()
    assert RunResult.model_validate_json(run.model_dump_json()).suite is None
    assert "suite" not in TypeAdapter(list[RunResult]).dump_python([run])[0]
    payload["suite"] = None
    assert RunResult.model_validate(payload).model_dump(mode="json") == run.model_dump(
        mode="json"
    )


@mark.parametrize(
    "changed_suite",
    (
        SUITE.model_copy(update={"name": "another-suite"}),
        SUITE.model_copy(update={"revision": 2}),
        SUITE.model_copy(update={"digest": "sha256:" + "a" * 64}),
    ),
)
def test_run_digest_pins_complete_suite_identity(changed_suite: ArtifactRef) -> None:
    pinned = run_result(suite=SUITE)
    changed = run_result(suite=changed_suite)
    assert pinned.result_digest != changed.result_digest
    assert pinned.result_digest != run_result().result_digest
    assert pinned.model_dump(mode="json")["suite"] == SUITE.model_dump(mode="json")
    assert RunResult.model_validate_json(pinned.model_dump_json()) == pinned

    payload = pinned.model_dump()
    payload["suite"] = changed_suite.model_dump()
    with raises(ValidationError, match="digest does not match"):
        RunResult.model_validate(payload)
    payload.pop("suite")
    with raises(ValidationError, match="digest does not match"):
        RunResult.model_validate(payload)


@mark.parametrize("invalid_suite", (DATASET, SUITE.model_copy(update={"digest": None})))
def test_run_suite_must_be_a_resolved_suite_artifact(
    invalid_suite: ArtifactRef,
) -> None:
    with raises(ValidationError, match="resolved suite artifact"):
        run_result(suite=invalid_suite)


def test_pinned_run_covers_execution_mode_including_fixture_mode() -> None:
    runs = [run_result(suite=SUITE, execution_mode=mode) for mode in ExecutionMode]
    # Pin the v3 projection, including the explicit execution mode for fixtures.
    assert [run.result_digest for run in runs] == [
        "sha256:bddae07868089c19cb407d8d6e29e4fa3ac963408c3da95b1d502f152a8d730c",
        "sha256:4ed253ba71ac022f056d820af3b8092e5cc48df69536d0cbe16ac657fdeaf085",
        "sha256:bb0644761f1431659e79e6039413b69978208a5a06f19cb002f958b531b22e35",
    ]
    for run in runs:
        payload = run.model_dump()
        payload["execution_mode"] = (
            ExecutionMode.LIVE
            if run.execution_mode is ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE
            else ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE
        )
        with raises(ValidationError, match="digest does not match"):
            RunResult.model_validate(payload)


def test_run_serializer_preserves_descriptive_schema_and_field_selection() -> None:
    properties = RunResult.model_json_schema(mode="serialization")["properties"]
    assert {"suite", "run_id", "result_digest"} <= properties.keys()
    assert run_result().model_dump(include={"run_id", "suite"}) == {"run_id": "run"}
    assert "suite" not in run_result(suite=SUITE).model_dump(exclude={"suite"})
