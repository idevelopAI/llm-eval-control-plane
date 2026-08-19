from pathlib import Path

from llm_eval_control_plane.domain import EvaluationSpec


def test_example_evaluation_specification_matches_public_contract() -> None:
    project_root = Path(__file__).parents[1]
    example = project_root / "examples" / "evaluation-spec.json"

    spec = EvaluationSpec.model_validate_json(example.read_text())

    assert spec.name == "databridge-release-candidate"
    assert len(spec.gates) == 3
