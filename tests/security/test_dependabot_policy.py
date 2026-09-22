"""Guard the intentional grouping boundaries in the dependency update policy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[2] / ".github" / "dependabot.yml"


def _group(config: str, name: str) -> str:
    match = re.search(
        rf"(?m)^      {re.escape(name)}:\n((?: {{8,}}[^\n]*\n|\n)*)",
        config,
    )
    assert match is not None, f"Missing dependency group: {name}"
    return match.group(1)


def _list_items(group: str, key: str) -> set[str]:
    match = re.search(
        rf"(?m)^        {re.escape(key)}:\n((?:          - [^\n]+\n)+)",
        group,
    )
    assert match is not None, f"Missing group list: {key}"
    return {
        line.strip().removeprefix("- ").strip("\"'")
        for line in match.group(1).splitlines()
    }


@pytest.mark.parametrize(
    ("name", "packages"),
    [
        (
            "react-runtime",
            {
                "react",
                "react-dom",
                "react-server-dom-webpack",
                "@types/react",
                "@types/react-dom",
            },
        ),
        ("next-framework", {"next", "eslint-config-next"}),
    ],
)
def test_coupled_runtime_packages_cross_dependency_types(
    name: str, packages: set[str]
) -> None:
    config = CONFIG.read_text(encoding="utf-8")
    group = _group(config, name)

    assert _list_items(group, "patterns") == packages
    assert "dependency-type:" not in group
    assert "update-types:" not in group
    assert config.index(f"      {name}:") < config.index(
        "      dashboard-development-dependencies:"
    )


@pytest.mark.parametrize(
    "name", ["dashboard-build-tooling", "dashboard-development-dependencies"]
)
def test_tooling_major_upgrades_are_not_batched(name: str) -> None:
    config = CONFIG.read_text(encoding="utf-8")
    group = _group(config, name)

    assert _list_items(group, "update-types") == {"minor", "patch"}
    assert "        dependency-type: development\n" in group
    assert "dependency-name: vitest" not in config


def test_build_tooling_does_not_fall_into_the_general_development_group() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    group = _group(config, "dashboard-build-tooling")

    assert _list_items(group, "patterns") == {
        "vite",
        "vinext",
        "@vitejs/*",
        "@cloudflare/*",
        "wrangler",
    }
    assert config.index("      dashboard-build-tooling:") < config.index(
        "      dashboard-development-dependencies:"
    )
