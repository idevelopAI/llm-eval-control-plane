from pydantic import ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
)


def case(
    case_id: str,
    *,
    input_value: object | None = None,
    expected: object | None = "answer",
    slices: tuple[str, ...] = ("language:en",),
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        input=CanonicalJson.from_value(
            {"question": case_id} if input_value is None else input_value
        ),
        expected=None if expected is None else CanonicalJson.from_value(expected),
        slices=slices,
    )


def test_dataset_normalizes_order_and_has_golden_content_digest() -> None:
    dataset = DatasetVersion.create(
        name="offline-fixture",
        revision=3,
        cases=(
            case("case-b", slices=("task:qa", "language:en")),
            case("case-a"),
        ),
    )

    assert [item.case_id for item in dataset.cases] == ["case-a", "case-b"]
    assert dataset.cases[1].slices == ("language:en", "task:qa")
    assert dataset.digest == (
        "sha256:78d639b3781c8c6cfa2e0ba1e6669a8098eb3f93f6382c55e9c706dcc37728a8"
    )
    assert dataset.artifact_ref.digest == dataset.digest


def test_dataset_digest_ignores_authoring_order_name_and_revision() -> None:
    first = DatasetVersion.create(
        name="first-name", revision=1, cases=(case("b"), case("a"))
    )
    second = DatasetVersion.create(
        name="second-name", revision=99, cases=(case("a"), case("b"))
    )

    assert first.digest == second.digest
    assert first.canonical_content_bytes() == second.canonical_content_bytes()


@mark.parametrize(
    "changed_case",
    [
        case("changed-id"),
        case("same", input_value={"question": "changed"}),
        case("same", expected="changed"),
        case("same", slices=("language:de",)),
    ],
)
def test_dataset_digest_changes_with_semantic_content(
    changed_case: EvaluationCase,
) -> None:
    original = DatasetVersion.create(name="dataset", revision=1, cases=(case("same"),))
    changed = DatasetVersion.create(name="dataset", revision=1, cases=(changed_case,))

    assert original.digest != changed.digest


def test_dataset_rejects_duplicate_case_ids_and_empty_cases() -> None:
    duplicate = case("duplicate")
    with raises(ValidationError, match="case IDs must be unique"):
        DatasetVersion.create(name="dataset", revision=1, cases=(duplicate, duplicate))
    with raises(ValidationError, match="at least 1 item"):
        DatasetVersion.create(name="dataset", revision=1, cases=())


def test_dataset_rejects_a_mismatched_declared_digest() -> None:
    with raises(ValidationError, match="digest does not match"):
        DatasetVersion(
            name="dataset",
            revision=1,
            digest="sha256:" + "0" * 64,
            cases=(case("case-1"),),
        )


def test_case_rejects_duplicate_slices() -> None:
    with raises(ValidationError, match="slice labels must be unique"):
        case("case-1", slices=("task:qa", "task:qa"))


def test_case_validates_numeric_and_schema_expectations() -> None:
    numeric = EvaluationCase(
        case_id="numeric",
        input=CanonicalJson.from_value(1),
        expected=CanonicalJson.from_value(1.5),
        numeric_tolerance=0.1,
    )
    schema = EvaluationCase(
        case_id="schema",
        input=CanonicalJson.from_value({}),
        expected_schema=CanonicalJson.from_value({"type": "object"}),
    )

    assert numeric.numeric_tolerance == 0.1
    assert schema.expected_schema is not None

    with raises(ValidationError, match="requires an expected value"):
        EvaluationCase(
            case_id="missing",
            input=CanonicalJson.from_value(1),
            numeric_tolerance=0.1,
        )
    with raises(ValidationError, match="requires a numeric expectation"):
        EvaluationCase(
            case_id="text",
            input=CanonicalJson.from_value(1),
            expected=CanonicalJson.from_value("one"),
            numeric_tolerance=0.1,
        )
    with raises(ValidationError, match="must be a JSON object"):
        EvaluationCase(
            case_id="bad-schema",
            input=CanonicalJson.from_value(1),
            expected_schema=CanonicalJson.from_value(["not", "an", "object"]),
        )
