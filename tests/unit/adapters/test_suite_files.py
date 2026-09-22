from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_eval_control_plane.adapters import import_dataset_jsonl
from llm_eval_control_plane.adapters.suite_files import (
    SuiteDefinition,
    read_suite,
    read_suite_definition,
    read_suite_scenarios,
    resolve_suite,
    write_suite,
)
from llm_eval_control_plane.domain import DatasetVersion, EvaluationSuiteVersion


@pytest.fixture
def suite_and_dataset() -> tuple[EvaluationSuiteVersion, DatasetVersion]:
    dataset = import_dataset_jsonl(
        ['{"case_id":"one","input":{"value":"private"},"expected":"private"}'],
        name="cases",
        revision=1,
    )
    definition = SuiteDefinition.model_validate(
        {
            "name": "protocol",
            "revision": 1,
            "dataset": {"name": "cases", "revision": 1},
            "evaluators": ["exact_match"],
            "gates": [
                {
                    "metric": "quality.exact_match",
                    "direction": "higher_is_better",
                    "threshold": 1.0,
                }
            ],
        }
    )
    return resolve_suite(definition, dataset), dataset


def test_resolution_checks_dataset_identity(
    suite_and_dataset: tuple[EvaluationSuiteVersion, DatasetVersion],
) -> None:
    _, dataset = suite_and_dataset
    definition = SuiteDefinition.model_validate(
        {
            "name": "protocol",
            "revision": 1,
            "dataset": {"name": "different", "revision": 1},
            "evaluators": ["exact_match"],
            "gates": [
                {
                    "metric": "quality.exact_match",
                    "direction": "higher_is_better",
                    "threshold": 1.0,
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="identity"):
        resolve_suite(definition, dataset)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file types")
def test_reads_reject_symlinks_and_non_regular_files(tmp_path: Path) -> None:
    regular = tmp_path / "original.json"
    regular.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(regular)
    with pytest.raises(OSError):
        read_suite_definition(link)
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        read_suite_definition(fifo)
    with pytest.raises(ValueError, match="regular file"):
        read_suite_definition(tmp_path)


def test_reads_reject_invalid_utf8_and_nonobject_suites(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        read_suite(path)
    path.write_text("[]")
    with pytest.raises(ValueError):
        read_suite(path)


def test_existing_destination_symlink_is_not_followed(
    tmp_path: Path, suite_and_dataset: tuple[EvaluationSuiteVersion, DatasetVersion]
) -> None:
    suite, _ = suite_and_dataset
    destination = tmp_path / "suite.json"
    original = tmp_path / "private.txt"
    original.write_text("private-sentinel")
    destination.symlink_to(original)
    with pytest.raises(FileExistsError):
        write_suite(destination, suite)
    assert original.read_text() == "private-sentinel"
    assert destination.is_symlink()
    assert list(tmp_path.glob(".suite-*")) == []


def test_failed_publication_removes_only_its_temporary_file(
    tmp_path: Path,
    suite_and_dataset: tuple[EvaluationSuiteVersion, DatasetVersion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite, _ = suite_and_dataset
    unrelated = tmp_path / ".suite-user-owned"
    unrelated.write_text("preserve")

    def fail_link(*args: object, **kwargs: object) -> None:
        raise OSError("private-sentinel")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError):
        write_suite(tmp_path / "suite.json", suite)
    assert not (tmp_path / "suite.json").exists()
    assert list(tmp_path.glob(".suite-*")) == [unrelated]


def test_write_enforces_size_limit_before_creating_file(
    tmp_path: Path,
    suite_and_dataset: tuple[EvaluationSuiteVersion, DatasetVersion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "llm_eval_control_plane.adapters.suite_files._MAX_DOCUMENT_BYTES", 1
    )
    with pytest.raises(ValueError, match="size limit"):
        write_suite(tmp_path / "suite.json", suite_and_dataset[0])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "document",
    ["[]", '{"one":false}', json.dumps({str(i): "echo" for i in range(1001)})],
    ids=["not-map", "not-string", "too-many"],
)
def test_scenario_overrides_are_bounded_string_mappings(
    tmp_path: Path, document: str
) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(document)
    with pytest.raises(ValueError):
        read_suite_scenarios(path)


def test_scenario_defaults_and_valid_mapping(tmp_path: Path) -> None:
    assert read_suite_scenarios(None) == {}
    path = tmp_path / "overrides.json"
    path.write_text('{"one":"echo"}')
    assert read_suite_scenarios(path) == {"one": "echo"}


def test_read_rechecks_size_after_file_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "growing.json"
    path.write_text("x" * 20)
    metadata = list(path.stat())
    metadata[6] = 0
    monkeypatch.setattr(os, "fstat", lambda descriptor: os.stat_result(metadata))
    monkeypatch.setattr(
        "llm_eval_control_plane.adapters.suite_files._MAX_DOCUMENT_BYTES", 8
    )
    with pytest.raises(ValueError, match="size limit"):
        read_suite(path)


def test_failed_stream_open_closes_descriptor_and_removes_temporary(
    tmp_path: Path,
    suite_and_dataset: tuple[EvaluationSuiteVersion, DatasetVersion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors: list[int] = []

    def fail_open(descriptor: int, *args: object, **kwargs: object) -> None:
        descriptors.append(descriptor)
        raise OSError("simulated stream failure")

    monkeypatch.setattr(os, "fdopen", fail_open)
    with pytest.raises(OSError):
        write_suite(tmp_path / "suite.json", suite_and_dataset[0])
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert list(tmp_path.iterdir()) == []
