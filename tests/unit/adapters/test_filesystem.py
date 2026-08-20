import asyncio
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch, raises

from llm_eval_control_plane.adapters import (
    BuiltInEvaluatorKind,
    CorruptRunError,
    DeterministicFakeTarget,
    FilesystemRunRepository,
    InvalidRunIdError,
    RunConflictError,
    RunNotFoundError,
    RunStoreError,
    build_evaluators,
)
from llm_eval_control_plane.application import InProcessRunner
from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    canonical_json_bytes,
)
from llm_eval_control_plane.domain.results import RunResult


class SequenceClock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def result(run_id: str = "run-001", *, value: str = "answer") -> RunResult:
    dataset = DatasetVersion.create(
        name="fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value({"scenario": "echo", "value": value}),
                expected=CanonicalJson.from_value(value),
            ),
        ),
    )
    return asyncio.run(
        InProcessRunner(clock=SequenceClock((0.0, 0.005))).run(
            run_id=run_id,
            dataset=dataset,
            target=DeterministicFakeTarget(),
            evaluators=build_evaluators((BuiltInEvaluatorKind.EXACT_MATCH,)),
        )
    )


def stored_path(root: Path, run_id: str) -> Path:
    for path in (root / "runs").glob("*.json"):
        document = json.loads(path.read_bytes())
        if document["result"]["run_id"] == run_id:
            return path
    raise AssertionError("stored run was not found")


def test_save_and_get_round_trip_with_owner_only_permissions(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    expected = result()

    repository.save(expected)

    path = stored_path(tmp_path, "run-001")
    assert repository.get("run-001") == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    expected = result()

    repository.save(expected)
    path = stored_path(tmp_path, "run-001")
    before = path.read_bytes()
    repository.save(expected)

    assert path.read_bytes() == before
    assert not list((tmp_path / "runs").glob(".tmp-*"))


def test_save_rejects_different_content_for_existing_run_id(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    repository.save(result(value="first"))

    with raises(RunConflictError, match="different content"):
        repository.save(result(value="second"))

    assert not list((tmp_path / "runs").glob(".tmp-*"))


def test_get_reports_missing_run_without_echoing_id(tmp_path: Path) -> None:
    with raises(RunNotFoundError, match="not found") as captured:
        FilesystemRunRepository(tmp_path).get("missing-run")

    assert "missing-run" not in str(captured.value)


def test_invalid_id_is_rejected_before_path_construction(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)

    with raises(InvalidRunIdError, match="invalid"):
        repository.get("../../private")

    assert not (tmp_path / "runs").exists()


def test_deterministic_storage_envelope_has_final_newline(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    expected = result()
    repository.save(expected)

    payload = stored_path(tmp_path, "run-001").read_bytes()
    document = json.loads(payload)

    assert payload.endswith(b"\n")
    assert document["storage_schema"] == "run/v1"
    assert document["result"]["result_digest"] == expected.result_digest

    second_root = tmp_path / "second"
    FilesystemRunRepository(second_root).save(expected)
    assert payload == stored_path(second_root, "run-001").read_bytes()


def test_get_loads_pre_execution_mode_run_artifacts(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    expected = result()
    repository.save(expected)
    path = stored_path(tmp_path, expected.run_id)
    document = json.loads(path.read_bytes())
    del document["result"]["execution_mode"]
    path.write_bytes(canonical_json_bytes(document) + b"\n")

    loaded = repository.get(expected.run_id)

    assert loaded == expected


def test_hashed_storage_keys_avoid_platform_filename_collisions(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)

    for run_id in ("Run", "run", "CON", "trailing."):
        repository.save(result(run_id=run_id))

    filenames = [path.name for path in (tmp_path / "runs").glob("*.json")]
    assert len(filenames) == len(set(filenames)) == 4
    assert all(name != "CON.json" and not name.endswith("..json") for name in filenames)
    assert all(
        repository.get(run_id).run_id == run_id
        for run_id in ("Run", "run", "CON", "trailing.")
    )


def test_atomic_publish_accepts_identical_concurrent_winner(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repository = FilesystemRunRepository(tmp_path)
    expected = result()

    def publish_first(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        raise FileExistsError

    monkeypatch.setattr(os, "link", publish_first)
    repository.save(expected)

    assert repository.get("run-001") == expected
    assert not list((tmp_path / "runs").glob(".tmp-*"))


def test_atomic_publish_rejects_different_concurrent_winner(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repository = FilesystemRunRepository(tmp_path)
    different = result(value="different")
    other_root = tmp_path / "other"
    FilesystemRunRepository(other_root).save(different)
    different_payload = stored_path(other_root, "run-001").read_bytes()

    def publish_different(_source: Path, destination: Path) -> None:
        destination.write_bytes(different_payload)
        raise FileExistsError

    monkeypatch.setattr(os, "link", publish_different)

    with raises(RunConflictError, match="different content"):
        repository.save(result())


def test_save_sanitizes_publish_errors_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    def deny_publish(_source: Path, _destination: Path) -> None:
        raise PermissionError("private filesystem detail")

    monkeypatch.setattr(os, "link", deny_publish)

    with raises(RunStoreError, match="Could not store") as captured:
        FilesystemRunRepository(tmp_path).save(result())

    assert "private filesystem detail" not in str(captured.value)
    assert not list((tmp_path / "runs").glob(".tmp-*"))


def test_save_closes_descriptor_when_permission_hardening_fails(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    original_fchmod = os.fchmod
    calls = 0

    def deny_temporary_permission(descriptor: int, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            original_fchmod(descriptor, mode)
            return
        raise PermissionError("private permission detail")

    monkeypatch.setattr(os, "fchmod", deny_temporary_permission)

    with raises(RunStoreError, match="Could not store"):
        FilesystemRunRepository(tmp_path).save(result())

    assert not list((tmp_path / "runs").glob(".tmp-*"))


def test_save_reports_directory_and_existing_artifact_read_errors(
    tmp_path: Path,
) -> None:
    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("not a directory", encoding="utf-8")
    with raises(RunStoreError, match="prepare run store"):
        FilesystemRunRepository(blocked_root).save(result())

    other_repository = FilesystemRunRepository(tmp_path / "other")
    other_repository.save(result())
    existing = stored_path(tmp_path / "other", "run-001")
    existing.unlink()
    existing.mkdir()
    with raises(CorruptRunError, match="could not be read"):
        other_repository.save(result())


def test_get_rejects_malformed_or_unknown_envelopes(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    repository.save(result())
    path = stored_path(tmp_path, "run-001")

    for payload in (
        b"not-json",
        canonical_json_bytes({"storage_schema": "run/v2", "result": {}}) + b"\n",
        canonical_json_bytes(
            {"storage_schema": "run/v1", "result": {}, "unknown": True}
        )
        + b"\n",
        b"\xff",
    ):
        path.write_bytes(payload)
        with raises(CorruptRunError, match="integrity validation"):
            repository.get("run-001")


def test_get_requires_exact_canonical_bytes_and_final_newline(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    repository.save(result())
    path = stored_path(tmp_path, "run-001")
    document = json.loads(path.read_bytes())

    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    with raises(CorruptRunError, match="integrity validation"):
        repository.get("run-001")

    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    with raises(CorruptRunError, match="integrity validation"):
        repository.get("run-001")


def test_get_rejects_tampered_digest_and_wrong_storage_key(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    repository.save(result())
    source = stored_path(tmp_path, "run-001")
    document = json.loads(source.read_bytes())

    document["result"]["result_digest"] = "sha256:" + "0" * 64
    source.write_text(json.dumps(document), encoding="utf-8")
    with raises(CorruptRunError, match="integrity validation"):
        repository.get("run-001")

    repository = FilesystemRunRepository(tmp_path / "other")
    repository.save(result(run_id="run-001"))
    repository.save(result(run_id="run-002"))
    wrong = stored_path(tmp_path / "other", "run-001")
    wrong.write_bytes(stored_path(tmp_path / "other", "run-002").read_bytes())
    with raises(CorruptRunError, match="integrity validation"):
        repository.get("run-001")


def test_get_rejects_non_regular_artifact(tmp_path: Path) -> None:
    repository = FilesystemRunRepository(tmp_path)
    repository.save(result())
    path = stored_path(tmp_path, "run-001")
    path.unlink()
    path.mkdir()

    with raises(CorruptRunError, match="could not be read"):
        repository.get("run-001")


def test_artifact_size_limit_is_enforced_for_writes_and_reads(tmp_path: Path) -> None:
    with raises(ValueError, match="positive integer"):
        FilesystemRunRepository(tmp_path, max_artifact_bytes=0)

    with raises(RunStoreError, match="size limit"):
        FilesystemRunRepository(tmp_path, max_artifact_bytes=8).save(result())

    repository = FilesystemRunRepository(tmp_path)
    repository.save(result())
    with raises(CorruptRunError, match="could not be read"):
        FilesystemRunRepository(tmp_path, max_artifact_bytes=8).get("run-001")


def test_read_limit_handles_a_file_that_grows_after_metadata_check(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repository = FilesystemRunRepository(tmp_path)
    repository.save(result())
    limited = FilesystemRunRepository(tmp_path, max_artifact_bytes=8)

    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=0),
    )
    with raises(CorruptRunError, match="could not be read"):
        limited.get("run-001")
