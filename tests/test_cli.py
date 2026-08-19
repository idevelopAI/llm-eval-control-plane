import json
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from llm_eval_control_plane import __version__
from llm_eval_control_plane.cli import app

runner = CliRunner()


def valid_specification() -> dict[str, object]:
    return {
        "name": "release-candidate",
        "dataset": {"kind": "dataset", "name": "cases", "revision": 1},
        "candidate": {"kind": "target", "name": "service", "revision": 2},
        "baseline": {"kind": "target", "name": "service", "revision": 1},
        "gates": [
            {
                "metric": "task.success_rate",
                "direction": "higher_is_better",
                "threshold": 0.9,
            }
        ],
    }


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_describes_truthful_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "schema" in result.stdout
    assert "validate" in result.stdout
    assert "run" not in result.stdout.lower()


def test_schema_command_returns_json_schema() -> None:
    result = runner.invoke(app, ["schema"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["title"] == "EvaluationSpec"
    assert payload["properties"]["schema_version"]["const"] == "1"


def test_validate_accepts_valid_specification(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_specification()))

    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "Valid evaluation specification: release-candidate"
    )


def test_validate_reports_errors_without_echoing_input(tmp_path: Path) -> None:
    spec_path = tmp_path / "invalid.json"
    spec_path.write_text(
        json.dumps(
            {
                **valid_specification(),
                "dataset": {
                    "kind": "target",
                    "name": "secret-sentinel",
                    "revision": 1,
                },
            }
        )
    )

    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 2
    assert "dataset must reference a dataset artifact" in result.stderr
    assert "secret-sentinel" not in result.stderr


def test_validate_reports_malformed_json(tmp_path: Path) -> None:
    spec_path = tmp_path / "broken.json"
    spec_path.write_text("not-json")

    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 2
    assert "Invalid evaluation specification" in result.stderr
    assert "Invalid JSON" in result.stderr


def test_validate_reports_unreadable_file(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_specification()))

    def fail_read_text(self: Path) -> str:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    result = runner.invoke(app, ["validate", str(spec_path)])

    assert result.exit_code == 2
    assert "Could not read specification: simulated read failure" in result.stderr
