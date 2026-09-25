"""Register one public synthetic echo case and exact-match suite on a local API."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from llm_eval_control_plane.api.contracts import DatasetCreateRequest
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor


def local_origin(value: str) -> str:
    """Reject credentials, redirects through paths, and non-loopback hosts."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "An explicit HTTP loopback origin with a port is required"
        ) from None
    return value.rstrip("/")


def starter_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(__file__).resolve().parents[1] / "examples/local-starter-dataset.json"
    dataset_body = json.loads(source.read_text(encoding="utf-8"))
    dataset = DatasetCreateRequest.model_validate(dataset_body).to_domain()
    contract = DeterministicEvaluationExecutor().validate_suite(
        adapter="deterministic_fake", evaluator_names=("exact_match",)
    )
    suite_body = {
        "name": "starter/echo",
        "revision": 1,
        "dataset": dataset.artifact_ref.model_dump(mode="json"),
        "evaluators": [item.model_dump(mode="json") for item in contract.evaluators],
        "execution": contract.execution.model_dump(mode="json"),
        "slices": [],
        "gates": [
            {
                "metric": "quality.exact_match",
                "direction": "higher_is_better",
                "threshold": 1.0,
            }
        ],
    }
    return dataset_body, suite_body


def register_starter(client: httpx.Client) -> None:
    """Create-once registration only; do not execute runs or inspect error bodies."""
    for route, body in zip(
        ("/v1/datasets", "/v1/suites"), starter_documents(), strict=True
    ):
        with client.stream("POST", route, json=body) as response:
            if response.status_code != 201:
                raise RuntimeError("Starter registration was not accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        origin = local_origin(args.origin)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.project):
            raise ValueError
        if not sys.stdin.isatty():
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Local project write credential (hidden): ")
        if not re.fullmatch(r"cpk_[A-Za-z0-9_-]{43}", token):
            raise ValueError
        with httpx.Client(
            base_url=origin,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {token}", "X-Project-ID": args.project},
        ) as client:
            register_starter(client)
    except (
        ValueError,
        RuntimeError,
        httpx.HTTPError,
        OSError,
        EOFError,
        getpass.GetPassWarning,
    ):
        print(
            "Starter registration failed. Check local API access "
            "and immutable revision conflicts. Use an interactive terminal."
        )
        return 2
    print(
        "Registered starter/echo r1: one synthetic case, one evaluator, one gate. "
        "No runs started."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
