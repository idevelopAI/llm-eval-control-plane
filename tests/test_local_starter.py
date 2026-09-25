from __future__ import annotations

import json
import warnings
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

from llm_eval_control_plane.api.contracts import (
    DatasetCreateRequest,
    SuiteCreateRequest,
)

_SPEC = spec_from_file_location(
    "seed_local_starter", Path(__file__).parents[1] / "scripts/seed_local_starter.py"
)
assert _SPEC is not None and _SPEC.loader is not None
starter = module_from_spec(_SPEC)
_SPEC.loader.exec_module(starter)


def test_starter_has_one_case_one_evaluator_and_one_gate() -> None:
    dataset_body, suite_body = starter.starter_documents()
    dataset = DatasetCreateRequest.model_validate(dataset_body).to_domain()
    suite = SuiteCreateRequest.model_validate(suite_body).to_domain()
    assert len(dataset.cases) == 1
    assert suite.dataset == dataset.artifact_ref
    assert len(suite.evaluators) == len(suite.gates) == 1
    assert suite.execution.execution_mode.value == "offline_mock"
    assert suite.evaluator_names == ("exact_match",)


@pytest.mark.parametrize(
    "origin",
    [
        "https://localhost:8000",
        "http://localhost.evil:8000",
        "http://example.com:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000/path",
        "http://user:pass@127.0.0.1:8000",
        "http://localhost:8000?private=1",
        "http://localhost:8000#private",
        "http://localhost:99999",
    ],
)
def test_starter_rejects_nonlocal_or_ambiguous_origins(origin: str) -> None:
    with pytest.raises(ValueError, match="HTTP loopback"):
        starter.local_origin(origin)


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8000/", "http://localhost:8000", "http://[::1]:8000"]
)
def test_starter_accepts_explicit_loopback_origins(origin: str) -> None:
    assert starter.local_origin(origin) == origin.rstrip("/")


def test_registration_only_posts_reviewed_fixture_documents() -> None:
    requests: list[httpx.Request] = []

    def accept(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    with httpx.Client(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(accept)
    ) as client:
        starter.register_starter(client)
    assert [request.url.path for request in requests] == ["/v1/datasets", "/v1/suites"]
    assert [json.loads(request.content) for request in requests] == list(
        starter.starter_documents()
    )


@pytest.mark.parametrize("status", [301, 401, 403, 409, 500])
def test_registration_stops_without_echoing_error_bodies(status: int) -> None:
    requests: list[httpx.Request] = []

    def reject(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, text="private-sentinel", headers={"Location": "https://example.com"}
        )

    with (
        httpx.Client(
            base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(reject)
        ) as client,
        pytest.raises(RuntimeError, match="not accepted") as caught,
    ):
        starter.register_starter(client)
    assert "private-sentinel" not in str(caught.value)
    assert len(requests) == 1


def test_noninteractive_registration_never_prompts_or_connects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(starter.sys, "argv", ["seed", "--project", "project-test"])
    monkeypatch.setattr(starter.sys.stdin, "isatty", lambda: False)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Noninteractive registration must stop before credentials or HTTP")

    monkeypatch.setattr(starter.getpass, "getpass", unexpected)
    monkeypatch.setattr(starter.httpx, "Client", unexpected)
    assert starter.main() == 2
    assert "interactive terminal" in capsys.readouterr().out


def test_unavailable_hidden_prompt_never_falls_back_to_echoed_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(starter.sys, "argv", ["seed", "--project", "project-test"])
    monkeypatch.setattr(starter.sys.stdin, "isatty", lambda: True)

    def unavailable(prompt: str) -> str:
        warnings.warn("Cannot hide input", starter.getpass.GetPassWarning, stacklevel=2)
        pytest.fail("Echoed input must never be requested")

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Unsafe prompt must stop before HTTP")

    monkeypatch.setattr(starter.getpass, "getpass", unavailable)
    monkeypatch.setattr(starter.httpx, "Client", unexpected)
    assert starter.main() == 2
    assert "registration failed" in capsys.readouterr().out
