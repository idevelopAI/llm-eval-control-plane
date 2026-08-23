import os
import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPOSITORY_ROOT / "scripts" / "export_openapi.py"
_COMMITTED = _REPOSITORY_ROOT / "docs" / "openapi-v1.json"


def _export(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source = str(_REPOSITORY_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if existing is None else f"{source}{os.pathsep}{existing}"
    )
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_committed_openapi_artifact_is_exact_and_deterministic(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    assert _export("--output", str(first)).returncode == 0
    assert _export("--output", str(second)).returncode == 0

    payload = _COMMITTED.read_bytes()
    assert first.read_bytes() == second.read_bytes() == payload
    assert payload.endswith(b"\n")
    assert not payload.endswith(b"\n\n")
    payload.decode("utf-8")


def test_export_check_detects_and_repairs_drift(tmp_path: Path) -> None:
    artifact = tmp_path / "openapi.json"
    artifact.write_bytes(b"{}\n")

    drifted = _export("--check", "--output", str(artifact))
    assert drifted.returncode == 1
    assert "out of date" in drifted.stdout

    assert _export("--output", str(artifact)).returncode == 0
    current = _export("--check", "--output", str(artifact))
    assert current.returncode == 0
    assert "current" in current.stdout
