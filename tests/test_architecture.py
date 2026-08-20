import ast
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "llm_eval_control_plane"


def imports_below(root: Path) -> Iterator[tuple[Path, str]]:
    for source_path in sorted(root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = (node.module,)
            else:
                continue
            for module in modules:
                yield source_path, module


def test_domain_does_not_depend_on_outer_or_infrastructure_layers() -> None:
    forbidden_roots = {
        "fastapi",
        "httpx",
        "opentelemetry",
        "redis",
        "sqlalchemy",
        "typer",
    }
    violations: list[str] = []

    for source_path, module in imports_below(PACKAGE_ROOT / "domain"):
        root = module.split(".", maxsplit=1)[0]
        if root in forbidden_roots:
            violations.append(f"{source_path.name}: imports {module}")
        if module.startswith("llm_eval_control_plane.") and not module.startswith(
            "llm_eval_control_plane.domain"
        ):
            violations.append(f"{source_path.name}: imports outer layer {module}")

    assert violations == []


def test_application_depends_only_inward() -> None:
    violations = [
        f"{source_path.name}: imports {module}"
        for source_path, module in imports_below(PACKAGE_ROOT / "application")
        if module == "llm_eval_control_plane.cli"
        or module.startswith("llm_eval_control_plane.adapters")
    ]

    assert violations == []
