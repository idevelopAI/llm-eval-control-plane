import json
from pathlib import Path

from typer.testing import CliRunner

from llm_eval_control_plane.adapters import (
    FilesystemRunRepository,
    export_dataset_jsonl,
    read_dataset_jsonl,
)
from llm_eval_control_plane.cli import app

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURE = PROJECT_ROOT / "examples" / "offline-100.jsonl"
DATASET_DIGEST = (
    "sha256:83296a96077826f7523365b6db509e06ebe056297fcba1b4203e59f63a4852f0"
)
RESULT_DIGEST = (
    "sha256:2544034c0247bd53c52b044496791d3e1b800c8153538b7db14885562cad3f58"
)


def test_checked_in_fixture_is_normalized_and_content_addressed() -> None:
    dataset = read_dataset_jsonl(FIXTURE, name="offline-100", revision=1)

    assert len(dataset.cases) == 100
    assert dataset.digest == DATASET_DIGEST
    assert export_dataset_jsonl(dataset) == FIXTURE.read_text(encoding="utf-8")


def test_one_offline_command_executes_and_persists_exactly_100_cases(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(FIXTURE),
            "--run-id",
            "offline-workflow-test",
            "--dataset-name",
            "offline-100",
            "--dataset-revision",
            "1",
            "--store",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["case_counts"] == {
        "attempted": 100,
        "completed": 100,
        "completed_with_errors": 0,
        "target_failed": 0,
    }
    assert summary["dataset_digest"] == DATASET_DIGEST
    assert summary["result_digest"] == RESULT_DIGEST
    assert summary["status"] == "completed"
    assert all(metric["errors"] == 0 for metric in summary["metrics"])

    stored = FilesystemRunRepository(tmp_path).get("offline-workflow-test")
    assert len(stored.cases) == 100
    assert stored.result_digest == RESULT_DIGEST
