from pathlib import Path

from llm_eval_control_plane.adapters import export_dataset_jsonl, read_dataset_jsonl
from llm_eval_control_plane.domain import EvaluationSpec


def test_example_evaluation_specification_matches_public_contract() -> None:
    project_root = Path(__file__).parents[1]
    example = project_root / "examples" / "evaluation-spec.json"

    spec = EvaluationSpec.model_validate_json(example.read_text())

    assert spec.name == "databridge-release-candidate"
    assert len(spec.gates) == 3


def test_release_gate_examples_are_pinned_and_canonical() -> None:
    project_root = Path(__file__).parents[1]
    spec_path = project_root / "examples" / "release-gate-spec.json"
    dataset_path = project_root / "examples" / "release-gate-40.jsonl"
    spec = EvaluationSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
    dataset = read_dataset_jsonl(
        dataset_path,
        name=spec.dataset.name,
        revision=spec.dataset.revision,
    )

    assert len(dataset.cases) == 40
    assert dataset.digest == spec.dataset.digest
    assert export_dataset_jsonl(dataset) == dataset_path.read_text(encoding="utf-8")
