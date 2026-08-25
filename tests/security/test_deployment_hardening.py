from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
GITLEAKS_CONFIG = PROJECT_ROOT / ".gitleaks.toml"
SECURITY_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "security-gate.yml"
DEPENDABOT_CONFIG = PROJECT_ROOT / ".github" / "dependabot.yml"
WORKFLOW_ROOT = PROJECT_ROOT / ".github" / "workflows"

_DIGEST_PIN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_ACTION_PIN = re.compile(r"^\s*uses:\s*[^\s@]+@([0-9a-f]{40})(?:\s+#.*)?$")
_IMAGE_DECLARATION = re.compile(r"^\s*image:\s*([^\s#]+)", re.MULTILINE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@cache
def _compose_config() -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")
    available = subprocess.run(
        [docker, "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if available.returncode != 0:
        pytest.skip("Docker Compose is unavailable")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CONTROL_PLANE_") and name != "COMPOSE_PROJECT_NAME"
    }
    rendered = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            ".env.example",
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=20,
    )
    assert rendered.returncode == 0, "Compose configuration must render"
    document = json.loads(rendered.stdout)
    assert isinstance(document, dict)
    return document


def test_external_container_and_workflow_inputs_are_immutable() -> None:
    dockerfile = _read(DOCKERFILE)
    references = [
        line.removeprefix("# syntax=").strip()
        for line in dockerfile.splitlines()
        if line.startswith("# syntax=")
    ]
    references.extend(
        line.split()[1] for line in dockerfile.splitlines() if line.startswith("FROM ")
    )

    for path in (COMPOSE_FILE, *sorted(WORKFLOW_ROOT.glob("*.yml"))):
        references.extend(
            reference
            for reference in _IMAGE_DECLARATION.findall(_read(path))
            if not reference.startswith("llm-eval-control-plane:")
        )

    assert references
    assert all(_DIGEST_PIN.fullmatch(reference) for reference in references)

    workflow_actions: list[str] = []
    for path in sorted(WORKFLOW_ROOT.glob("*.yml")):
        for line in _read(path).splitlines():
            if "uses:" not in line:
                continue
            match = _ACTION_PIN.fullmatch(line)
            assert match is not None, f"Action is not full-SHA pinned in {path.name}"
            workflow_actions.append(match.group(1))
    assert workflow_actions


def test_runtime_image_ends_as_a_fixed_non_root_user() -> None:
    stages = re.split(r"(?m)^FROM\s+", _read(DOCKERFILE))[1:]
    assert stages
    runtime_stage = stages[-1]
    users = re.findall(r"(?m)^USER\s+([^\s]+)\s*$", runtime_stage)

    assert users == ["10001:10001"]
    assert "adduser" in runtime_stage
    assert "--no-create-home" in runtime_stage
    assert "--shell /sbin/nologin" in runtime_stage
    assert "python -m pip uninstall --yes pip" in runtime_stage


def test_dependabot_covers_every_pinned_dependency_source() -> None:
    ecosystems = set(
        re.findall(
            r"(?m)^\s+- package-ecosystem:\s+([^\s]+)\s*$",
            _read(DEPENDABOT_CONFIG),
        )
    )

    assert ecosystems == {"docker", "docker-compose", "github-actions", "uv"}


def test_build_context_excludes_secrets_evidence_and_vcs_metadata() -> None:
    exclusions = {
        line.strip()
        for line in _read(DOCKERIGNORE).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".git", ".github", ".env", ".env.*", ".secrets", ".llm-eval"} <= (
        exclusions
    )
    assert "!.env.example" in exclusions


def test_application_services_are_read_only_and_capability_free() -> None:
    services = _compose_config()["services"]
    for service_name in ("api", "migrate", "worker"):
        service = services[service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        tmpfs = service["tmpfs"]
        assert any(
            entry.startswith("/tmp:")
            and all(
                option in entry.split(",") for option in ("noexec", "nosuid", "nodev")
            )
            for entry in tmpfs
        )


def test_networks_keep_workers_and_database_private_and_api_on_loopback() -> None:
    config = _compose_config()
    services = config["services"]
    assert config["networks"]["control-plane"]["internal"] is True

    for service_name in ("database", "migrate", "worker"):
        service = services[service_name]
        assert set(service["networks"]) == {"control-plane"}
        assert not service.get("ports")

    api = services["api"]
    assert set(api["networks"]) == {"api-edge", "control-plane"}
    assert api["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8000,
            "published": "8000",
            "protocol": "tcp",
        }
    ]


def test_runtime_credentials_are_file_mounted_without_literal_values() -> None:
    config = _compose_config()
    secret_name = "control_plane_database_password"
    secret = config["secrets"][secret_name]
    assert Path(secret["file"]).name == "postgres-password.txt"
    assert ".secrets" in Path(secret["file"]).parts

    services = config["services"]
    for service_name in ("database", "migrate", "worker"):
        mounts = services[service_name]["secrets"]
        assert mounts == [{"source": secret_name, "target": secret_name}]
    assert {
        (mount["source"], mount["target"]) for mount in services["api"]["secrets"]
    } == {
        ("control_plane_auth_config", "control_plane_auth_config"),
        (secret_name, secret_name),
    }
    auth_secret = config["secrets"]["control_plane_auth_config"]
    assert Path(auth_secret["file"]).name == "control-plane-auth.json"
    assert ".secrets" in Path(auth_secret["file"]).parts
    assert services["api"]["environment"]["CONTROL_PLANE_AUTH_FILE"] == (
        "/run/secrets/control_plane_auth_config"
    )
    for service_name in ("database", "migrate", "worker"):
        assert "CONTROL_PLANE_AUTH_FILE" not in services[service_name]["environment"]

    database_environment = services["database"]["environment"]
    assert database_environment["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/control_plane_database_password"
    )
    assert "POSTGRES_PASSWORD" not in database_environment

    for service_name in ("api", "migrate", "worker"):
        environment = services[service_name]["environment"]
        assert environment["CONTROL_PLANE_DATABASE_PASSWORD_FILE"] == (
            "/run/secrets/control_plane_database_password"
        )
        assert "CONTROL_PLANE_DATABASE_URL" not in environment


def test_security_workflow_is_redacted_pinned_and_least_privilege() -> None:
    workflow = _read(SECURITY_WORKFLOW)
    gitleaks_config = _read(GITLEAKS_CONFIG)
    expected_checks = {
        "Dependency Vulnerability Audit",
        "Static Security Analysis",
        "Secret History Scan",
        "Container Security Gate",
        "CodeQL Python",
    }
    actual_checks = set(re.findall(r"(?m)^    name: (.+)$", workflow))

    assert expected_checks <= actual_checks
    assert "permissions:\n  contents: read" in workflow
    assert workflow.count("security-events: write") == 1
    assert "secrets." not in workflow
    assert "upload-artifact" not in workflow
    assert not any(
        permission in workflow
        for permission in (
            "actions: write",
            "contents: write",
            "id-token: write",
            "packages: write",
            "pull-requests: write",
        )
    )
    assert "--config=.gitleaks.toml" in workflow
    assert "--redact=100" in workflow
    assert '--log-opts="--all"' in workflow
    assert "v0.72.0" in workflow
    assert 'ignore-unfixed: "false"' in workflow
    assert 'ignore-unfixed: "true"' in workflow
    assert "security-extended" in workflow
    assert "uv sync --locked --group security --no-install-project" in workflow
    assert "uv run --no-sync pip-audit" in workflow

    reviewed_fixture_commits = {
        "54bddf634893106d7b670c0c2c8f4a2ba6a93702",
        "2cca78796618c551ac0142757c0fd28734b40d28",
        "793ceb127cf2b096bf078fa3b41e8b9346736585",
        "7dc3608ec2f583d8d5ba1523c8c8220805105ad7",
    }
    assert "useDefault = true" in gitleaks_config
    assert 'id = "generic-api-key"' in gitleaks_config
    assert "[[rules.allowlists]]" in gitleaks_config
    assert set(re.findall(r'"([0-9a-f]{40})"', gitleaks_config)) == (
        reviewed_fixture_commits
    )
    assert not any(
        broad_allowlist in gitleaks_config
        for broad_allowlist in ("paths =", "regexes =", "stopwords =")
    )
