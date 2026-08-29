from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_ROOT = PROJECT_ROOT / "dashboard"
APP_ROOT = DASHBOARD_ROOT / "app"
HOSTING_CONFIG = DASHBOARD_ROOT / ".openai" / "hosting.json"

_ROUTE_HANDLER_SUFFIXES = {
    ".cjs",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".mts",
    ".ts",
    ".tsx",
}
_PUBLIC_ENV_NAME = re.compile(r"\b(?:NEXT_PUBLIC|VITE)_[A-Z0-9_]+\b")
_SERVER_ONLY_MODULES = (
    DASHBOARD_ROOT / "src" / "server" / "dashboard-read-executor.ts",
    DASHBOARD_ROOT / "src" / "server" / "hosted-config.ts",
    DASHBOARD_ROOT / "src" / "server" / "hosted-read-handler.ts",
    DASHBOARD_ROOT / "src" / "server" / "hosted-read-response.ts",
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"cpk_[A-Za-z0-9_-]{43}"),
    re.compile(r"Bearer [A-Za-z0-9._~-]{32,}"),
)


def _production_source_files() -> tuple[Path, ...]:
    sources = [
        path
        for root in (APP_ROOT, DASHBOARD_ROOT / "src")
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".css", ".js", ".jsx", ".json", ".mjs", ".ts", ".tsx"}
        and ".test." not in path.name
        and ".spec." not in path.name
    ]
    sources.extend(
        path
        for path in (
            HOSTING_CONFIG,
            DASHBOARD_ROOT / "next.config.ts",
            DASHBOARD_ROOT / "vite.config.ts",
        )
        if path.is_file()
    )
    return tuple(sorted(set(sources)))


def test_hosted_control_plane_boundary_remains_disabled() -> None:
    route_handlers = sorted(
        path.relative_to(DASHBOARD_ROOT)
        for path in APP_ROOT.rglob("route.*")
        if path.suffix in _ROUTE_HANDLER_SUFFIXES
    )
    assert not route_handlers, (
        f"Hosted route handlers activate the boundary: {route_handlers}"
    )

    hosting = json.loads(HOSTING_CONFIG.read_text(encoding="utf-8"))
    assert hosting["d1"] is None
    assert hosting["r2"] is None

    public_names: dict[str, list[str]] = {}
    for path in _production_source_files():
        names = sorted(set(_PUBLIC_ENV_NAME.findall(path.read_text(encoding="utf-8"))))
        if names:
            public_names[str(path.relative_to(PROJECT_ROOT))] = names
    assert not public_names, (
        f"Public dashboard configuration is forbidden: {public_names}"
    )

    for module in _SERVER_ONLY_MODULES:
        assert module.read_text(encoding="utf-8").startswith(
            "import 'server-only';\n"
        ), (
            f"Private hosted module lost its server-only marker: "
            f"{module.relative_to(PROJECT_ROOT)}"
        )


def test_hosted_dashboard_production_sources_have_no_credentials() -> None:
    matches: list[str] = []
    for path in _production_source_files():
        contents = path.read_text(encoding="utf-8")
        if any(pattern.search(contents) for pattern in _CREDENTIAL_PATTERNS):
            matches.append(str(path.relative_to(PROJECT_ROOT)))

    assert not matches, (
        f"Credential-like literals found in production sources: {matches}"
    )
