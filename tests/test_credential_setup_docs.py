"""Exercise the published bootstrap without real credentials or a running API."""

from __future__ import annotations

import getpass
import os
import re
import runpy
import secrets
import stat
import warnings
from pathlib import Path

import pytest
from starlette.types import Scope

from llm_eval_control_plane.api.security import (
    ControlPlaneAuthorizer,
    digest_token,
    load_authentication_configuration,
)

GUIDE = Path(__file__).parents[1] / "docs/operations/local-credentials.md"


@pytest.fixture
def credentials() -> tuple[str, str]:
    return "cpk_" + secrets.token_urlsafe(32), "cpk_" + secrets.token_urlsafe(32)


def _run_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, project_id: str = "local-eval"
) -> None:
    match = re.search(
        r"```bash\nuv run --locked python - <<'PY'\n(.*?)\nPY\n```",
        GUIDE.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert match is not None, "The documented bootstrap must remain executable"
    source = match.group(1).replace(
        'project_id = "local-eval"', f"project_id = {project_id!r}"
    )
    script = tmp_path / "bootstrap.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    previous_mask = os.umask(0o077)
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        os.umask(previous_mask)


def _answers(monkeypatch: pytest.MonkeyPatch, values: tuple[str, str]) -> None:
    answers = iter(values)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))


def _scope(token: str, method: str, path: str, project: str = "local-eval") -> Scope:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [
            (b"authorization", f"Bearer {token}".encode("ascii")),
            (b"x-project-id", project.encode("ascii")),
        ],
        "state": {},
    }


def test_bootstrap_persists_only_digests_and_enforces_separate_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    credentials: tuple[str, str],
) -> None:
    _answers(monkeypatch, credentials)
    _run_bootstrap(tmp_path, monkeypatch)
    path = tmp_path / ".secrets/control-plane-auth.json"
    configuration = load_authentication_configuration(path)
    assert configuration.project_id == "local-eval"
    assert [item.principal_id for item in configuration.principals] == [
        "dashboard-reader",
        "local-operator",
    ]
    assert tuple(item.token_digest for item in configuration.principals) == tuple(
        digest_token(value) for value in credentials
    )
    authorizer = ControlPlaneAuthorizer(configuration)
    reader, operator = credentials
    for token in credentials:
        assert authorizer.authorize(_scope(token, "GET", "/v1/suites")) is None
        for method, route in [
            ("GET", "/metrics"),
            ("POST", "/v1/jobs/job/cancellation"),
        ]:
            failure = authorizer.authorize(_scope(token, method, route))
            assert failure is not None and failure.status == 403
        failure = authorizer.authorize(_scope(token, "GET", "/v1/suites", "wrong"))
        assert failure is not None and failure.status == 403
    failure = authorizer.authorize(_scope(reader, "POST", "/v1/suite-runs"))
    assert failure is not None and failure.status == 403
    assert authorizer.authorize(_scope(operator, "POST", "/v1/suite-runs")) is None
    output = capsys.readouterr()
    persisted = path.read_text(encoding="utf-8")
    for value in credentials:
        assert value not in persisted + output.out + output.err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("invalid", ["reader", "operator", "duplicate", "project"])
def test_invalid_setup_does_not_write_a_registry(
    invalid: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    credentials: tuple[str, str],
) -> None:
    reader, operator = credentials
    values = {
        "reader": ("not-a-credential", operator),
        "operator": (reader, "not-a-credential"),
        "duplicate": (reader, reader),
        "project": credentials,
    }[invalid]
    _answers(monkeypatch, values)
    with pytest.raises(SystemExit, match="Setup stopped") as failure:
        _run_bootstrap(
            tmp_path,
            monkeypatch,
            project_id="invalid project" if invalid == "project" else "local-eval",
        )
    assert not (tmp_path / ".secrets/control-plane-auth.json").exists()
    output = capsys.readouterr()
    for value in values:
        assert value not in str(failure.value) + output.out + output.err


@pytest.mark.parametrize("problem", ["echo", "eof", "interrupt"])
def test_unavailable_hidden_input_fails_closed(
    problem: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_prompt: str) -> str:
        if problem == "echo":
            warnings.warn("Cannot hide input", getpass.GetPassWarning, stacklevel=2)
            pytest.fail("Echoed input must never be attempted")
        if problem == "eof":
            raise EOFError
        raise KeyboardInterrupt

    monkeypatch.setattr(getpass, "getpass", unavailable)
    with pytest.raises(SystemExit, match="Setup stopped"):
        _run_bootstrap(tmp_path, monkeypatch)
    assert not (tmp_path / ".secrets/control-plane-auth.json").exists()


@pytest.mark.parametrize("existing", ["file", "file-symlink", "directory-symlink"])
def test_existing_registry_or_symlink_is_not_overwritten(
    existing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "preserve.txt"
    sentinel.write_text("preserve existing configuration", encoding="utf-8")
    directory = tmp_path / ".secrets"
    if existing == "directory-symlink":
        destination = tmp_path / "other"
        destination.mkdir()
        directory.symlink_to(destination, target_is_directory=True)
    else:
        directory.mkdir()
        target = directory / "control-plane-auth.json"
        if existing == "file-symlink":
            target.symlink_to(sentinel)
        else:
            target.write_text("preserve existing configuration", encoding="utf-8")

    def unexpected_prompt(_prompt: str) -> str:
        pytest.fail("Existing files must be rejected before asking for credentials")

    monkeypatch.setattr(getpass, "getpass", unexpected_prompt)
    with pytest.raises(SystemExit, match="Setup stopped"):
        _run_bootstrap(tmp_path, monkeypatch)
    assert sentinel.read_text(encoding="utf-8") == "preserve existing configuration"
    if existing == "file":
        assert (directory / "control-plane-auth.json").read_text(encoding="utf-8") == (
            "preserve existing configuration"
        )
