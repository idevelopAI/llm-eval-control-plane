import json

from pydantic import ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    EvaluationSuiteVersion,
    ExecutionMode,
    MetricDirection,
    MetricGate,
    SuiteCaseOrder,
    SuiteEvaluator,
    SuiteExecutionSettings,
)


def artifact(
    kind: ArtifactKind,
    name: str,
    *,
    revision: int = 1,
    digest_character: str = "a",
    resolved: bool = True,
) -> ArtifactRef:
    return ArtifactRef(
        kind=kind,
        name=name,
        revision=revision,
        digest=("sha256:" + digest_character * 64) if resolved else None,
    )


def evaluator(
    executor_name: str = "exact_match",
    *,
    artifact_name: str = "builtin/exact_match",
    revision: int = 1,
    digest_character: str = "b",
    metrics: tuple[str, ...] = ("quality.exact_match",),
) -> SuiteEvaluator:
    return SuiteEvaluator(
        executor_name=executor_name,
        artifact=artifact(
            ArtifactKind.EVALUATOR,
            artifact_name,
            revision=revision,
            digest_character=digest_character,
        ),
        metrics=metrics,
    )


def gate(
    metric: str = "quality.exact_match",
    *,
    slice_name: str | None = None,
    threshold: float = 0.9,
    direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER,
    allowed_regression: float = 0.01,
) -> MetricGate:
    return MetricGate(
        metric=metric,
        slice=slice_name,
        direction=direction,
        threshold=threshold,
        allowed_regression=allowed_regression,
    )


def execution(**updates: object) -> SuiteExecutionSettings:
    values: dict[str, object] = {
        "adapter": "deterministic_fake",
        "execution_mode": ExecutionMode.OFFLINE_MOCK,
    }
    values.update(updates)
    return SuiteExecutionSettings.model_validate(values)


def valid_suite(**updates: object) -> EvaluationSuiteVersion:
    values: dict[str, object] = {
        "name": "release/core",
        "revision": 3,
        "dataset": artifact(ArtifactKind.DATASET, "release/cases"),
        "evaluators": (evaluator(),),
        "slices": ("language/de",),
        "execution": execution(),
        "gates": (gate(),),
    }
    values.update(updates)
    return EvaluationSuiteVersion.create(**values)  # type: ignore[arg-type]


def test_suite_normalizes_content_and_has_golden_digest() -> None:
    exact_match = evaluator(
        metrics=("quality.normalized_match", "quality.exact_match"),
    )
    usage = evaluator(
        "usage",
        artifact_name="builtin/usage",
        digest_character="c",
        metrics=("usage.total_units",),
    )
    suite = valid_suite(
        evaluators=(usage, exact_match),
        slices=("safety/refusal", "language/de"),
        gates=(
            gate("usage.total_units", direction=MetricDirection.LOWER_IS_BETTER),
            gate(slice_name="language/de"),
            gate(),
        ),
    )

    assert suite.evaluator_names == ("exact_match", "usage")
    assert suite.evaluator_refs == (exact_match.artifact, usage.artifact)
    assert suite.evaluators[0].metrics == (
        "quality.exact_match",
        "quality.normalized_match",
    )
    assert suite.slices == ("language/de", "safety/refusal")
    assert [(item.metric, item.slice) for item in suite.gates] == [
        ("quality.exact_match", None),
        ("quality.exact_match", "language/de"),
        ("usage.total_units", None),
    ]
    assert suite.digest == (
        "sha256:2793ce0e5468ab4ed1a70ef3b0a72e1ad41bd798b289b419307577a4cb3928d4"
    )
    assert suite.artifact_ref == ArtifactRef(
        kind=ArtifactKind.SUITE,
        name="release/core",
        revision=3,
        digest=suite.digest,
    )


def test_suite_digest_ignores_name_revision_and_authoring_order() -> None:
    first_evaluator = evaluator()
    second_evaluator = evaluator(
        "latency",
        artifact_name="builtin/latency",
        digest_character="c",
        metrics=("latency.ms",),
    )
    first = valid_suite(
        name="first",
        revision=1,
        evaluators=(first_evaluator, second_evaluator),
        slices=("task/qa", "language/de"),
        gates=(gate(), gate("latency.ms", slice_name="task/qa")),
    )
    second = valid_suite(
        name="second",
        revision=99,
        evaluators=(second_evaluator, first_evaluator),
        slices=("language/de", "task/qa"),
        gates=(gate("latency.ms", slice_name="task/qa"), gate()),
    )

    assert first.digest == second.digest
    assert first.canonical_content_bytes() == second.canonical_content_bytes()


@mark.parametrize(
    ("field", "changed_value"),
    [
        (
            "dataset",
            artifact(
                ArtifactKind.DATASET,
                "release/cases",
                digest_character="f",
            ),
        ),
        (
            "evaluators",
            (evaluator(digest_character="f"),),
        ),
        (
            "execution",
            execution(adapter="another_adapter"),
        ),
        (
            "execution",
            execution(
                execution_mode=ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
            ),
        ),
        (
            "slices",
            ("task/qa",),
        ),
        (
            "gates",
            (gate(threshold=0.8),),
        ),
    ],
)
def test_suite_digest_changes_with_semantic_content(
    field: str,
    changed_value: object,
) -> None:
    original = valid_suite()
    updates: dict[str, object] = {field: changed_value}
    if field == "slices":
        updates["gates"] = (gate(),)
    changed = valid_suite(**updates)

    assert changed.digest != original.digest


def test_suite_round_trips_through_json_and_is_deeply_frozen() -> None:
    suite = valid_suite()

    restored = EvaluationSuiteVersion.model_validate_json(suite.model_dump_json())

    assert restored == suite
    assert restored.schema_version == "1"
    with raises(ValidationError, match="Instance is frozen"):
        restored.execution.adapter = "changed"


def test_suite_rejects_a_mismatched_declared_digest() -> None:
    suite = valid_suite()

    with raises(ValidationError, match="suite digest does not match"):
        EvaluationSuiteVersion.model_validate(
            {
                **suite.model_dump(mode="python"),
                "digest": "sha256:" + "0" * 64,
            }
        )


@mark.parametrize(
    "dataset",
    [
        artifact(ArtifactKind.TARGET, "wrong-kind"),
        artifact(ArtifactKind.DATASET, "unresolved", resolved=False),
    ],
)
def test_suite_requires_a_resolved_dataset_reference(dataset: ArtifactRef) -> None:
    message = (
        "dataset artifact" if dataset.kind is not ArtifactKind.DATASET else "resolved"
    )
    with raises(ValidationError, match=message):
        valid_suite(dataset=dataset)


@mark.parametrize(
    "evaluator_ref",
    [
        artifact(ArtifactKind.TARGET, "wrong-kind"),
        artifact(ArtifactKind.EVALUATOR, "unresolved", resolved=False),
    ],
)
def test_suite_evaluator_requires_a_resolved_evaluator_reference(
    evaluator_ref: ArtifactRef,
) -> None:
    message = (
        "evaluator artifact"
        if evaluator_ref.kind is not ArtifactKind.EVALUATOR
        else "resolved"
    )
    with raises(ValidationError, match=message):
        SuiteEvaluator(
            executor_name="exact_match",
            artifact=evaluator_ref,
            metrics=("quality.exact_match",),
        )


def test_suite_rejects_duplicate_evaluator_bindings() -> None:
    first = evaluator()
    duplicate_executor = evaluator(
        artifact_name="builtin/other",
        digest_character="c",
        metrics=("quality.other",),
    )
    with raises(ValidationError, match="executor names must be unique"):
        valid_suite(evaluators=(first, duplicate_executor))

    duplicate_artifact = evaluator(
        "other_executor",
        metrics=("quality.other",),
    )
    with raises(ValidationError, match="artifacts must be unique"):
        valid_suite(evaluators=(first, duplicate_artifact))


def test_suite_rejects_duplicate_evaluator_metrics() -> None:
    with raises(ValidationError, match="evaluator metrics must be unique"):
        evaluator(metrics=("quality.exact_match", "quality.exact_match"))

    first = evaluator()
    duplicate_metric = evaluator(
        "other_executor",
        artifact_name="builtin/other",
        digest_character="c",
    )
    with raises(ValidationError, match="globally unique"):
        valid_suite(evaluators=(first, duplicate_metric))


def test_suite_rejects_duplicate_slices_and_gates() -> None:
    with raises(ValidationError, match="suite slices must be unique"):
        valid_suite(slices=("language/de", "language/de"))
    with raises(ValidationError, match="gate metric and slice combinations"):
        valid_suite(gates=(gate(), gate()))


def test_suite_gates_must_use_declared_metrics_and_slices() -> None:
    with raises(ValidationError, match="metric must be emitted"):
        valid_suite(gates=(gate("quality.unknown"),))
    with raises(ValidationError, match="slice must be declared"):
        valid_suite(gates=(gate(slice_name="task/qa"),))


def test_suite_requires_evaluators_and_release_gates() -> None:
    with raises(ValidationError, match="at least 1 item"):
        valid_suite(evaluators=())
    with raises(ValidationError, match="at least 1 item"):
        valid_suite(gates=())


def test_suite_execution_contract_is_explicit_and_rejects_secrets() -> None:
    settings = execution()

    assert settings.case_order is SuiteCaseOrder.CASE_ID_ASCENDING
    assert settings.invocations_per_case == 1
    assert settings.max_concurrency == 1
    with raises(ValidationError, match="Input should be 1"):
        execution(max_concurrency=2)
    with raises(ValidationError, match="Extra inputs are not permitted"):
        execution(api_key="must-not-enter-suite-content")


def test_suite_compiles_the_existing_target_bound_evaluation_spec() -> None:
    suite = valid_suite()
    baseline = artifact(ArtifactKind.TARGET, "model", revision=1)
    candidate = artifact(ArtifactKind.TARGET, "model", revision=2)

    spec = suite.to_evaluation_spec(baseline=baseline, candidate=candidate)

    assert spec.name == suite.name
    assert spec.dataset == suite.dataset
    assert spec.baseline == baseline
    assert spec.candidate == candidate
    assert spec.gates == suite.gates


def test_suite_canonical_content_contains_no_registration_metadata() -> None:
    suite = valid_suite()
    content = json.loads(suite.canonical_content_bytes())

    assert content["digest_schema"] == "evaluation-suite/v1"
    assert "name" not in content
    assert "revision" not in content
    assert "schema_version" not in content
    assert "digest" not in content
    assert content["execution"] == {
        "adapter": "deterministic_fake",
        "case_order": "case_id_ascending",
        "execution_mode": "offline_mock",
        "invocations_per_case": 1,
        "max_concurrency": 1,
    }
