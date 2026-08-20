from io import StringIO
from pathlib import Path

from pytest import mark, raises

from llm_eval_control_plane.adapters import (
    DatasetImportError,
    export_dataset_jsonl,
    import_dataset_jsonl,
    read_dataset_jsonl,
    write_dataset_jsonl,
)
from llm_eval_control_plane.domain import CanonicalJson, DatasetVersion, EvaluationCase


def record(case_id: str, value: object = "answer") -> str:
    return (
        '{"case_id":"'
        + case_id
        + '","input":{"scenario":"echo","value":"answer"},'
        + '"expected":'
        + CanonicalJson.from_value(value).canonical
        + ',"slices":["task:echo"]}'
    )


def test_jsonl_import_normalizes_format_case_order_and_blank_lines() -> None:
    source = [
        "\n",
        ' { "slices": ["task:echo"], "expected": "answer", '
        '"input": {"value":"answer","scenario":"echo"}, "case_id": "b" }\n',
        record("a"),
    ]

    dataset = import_dataset_jsonl(source, name="fixture", revision=1)

    assert [item.case_id for item in dataset.cases] == ["a", "b"]
    assert dataset.digest.startswith("sha256:")


def test_jsonl_formatting_and_order_do_not_change_digest() -> None:
    first = import_dataset_jsonl([record("b"), record("a")], name="first", revision=1)
    second = import_dataset_jsonl([record("a"), record("b")], name="second", revision=9)

    assert first.digest == second.digest
    assert export_dataset_jsonl(first) == export_dataset_jsonl(second)


def test_jsonl_export_is_sorted_compact_and_round_trips() -> None:
    dataset = import_dataset_jsonl(
        [record("b"), record("a")], name="fixture", revision=1
    )

    exported = export_dataset_jsonl(dataset)
    stream = StringIO()
    write_dataset_jsonl(dataset, stream)
    restored = import_dataset_jsonl(
        exported.splitlines(keepends=True), name="fixture", revision=1
    )

    assert stream.getvalue() == exported
    assert exported.endswith("\n") and not exported.endswith("\n\n")
    assert exported.splitlines()[0].startswith('{"case_id":"a"')
    assert restored == dataset


@mark.parametrize(
    ("source", "code"),
    [
        (["not-json"], "invalid_json"),
        (["[]"], "case_not_object"),
        ([record("a"), record("a")], "duplicate_case_id"),
        (["\n", "  \n"], "empty_dataset"),
        (['{"case_id":"a","case_id":"b"}'], "duplicate_key"),
        (["\ufeff" + record("a")], "bom"),
        (['{"case_id":"a","input":{},"unknown":true}'], "invalid_case"),
        (['{"case_id":"a","input":NaN}'], "non_finite_number"),
    ],
)
def test_jsonl_import_returns_structured_content_safe_errors(
    source: list[str], code: str
) -> None:
    with raises(DatasetImportError) as raised:
        import_dataset_jsonl(source, name="fixture", revision=1)

    assert raised.value.code == code
    assert "private-sentinel" not in str(raised.value)


def test_jsonl_error_tracks_physical_line_and_json_column() -> None:
    with raises(DatasetImportError) as raised:
        import_dataset_jsonl(
            ["\n", record("a"), '{"case_id": }'],
            name="fixture",
            revision=1,
        )

    assert raised.value.line == 3
    assert raised.value.column == 13


def test_jsonl_metadata_errors_are_sanitized() -> None:
    with raises(DatasetImportError) as raised:
        import_dataset_jsonl([record("a")], name=" invalid", revision=0)

    assert raised.value.code == "invalid_dataset"
    assert "invalid" not in str(raised.value)


def test_jsonl_path_reader_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_bytes(b"\xff\xfe")

    with raises(DatasetImportError) as raised:
        read_dataset_jsonl(path, name="fixture", revision=1)

    assert raised.value.code == "invalid_utf8"


def test_case_export_includes_every_semantic_default() -> None:
    dataset = DatasetVersion.create(
        name="fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="a",
                input=CanonicalJson.from_value({"scenario": "echo", "value": 1}),
            ),
        ),
    )

    exported = export_dataset_jsonl(dataset)
    assert '"expected":null' in exported
    assert '"expected_refusal":false' in exported
    assert '"numeric_tolerance":null' in exported
