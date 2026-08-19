import ast
from pathlib import Path


def test_domain_does_not_depend_on_outer_layers() -> None:
    project_root = Path(__file__).parents[1]
    domain_root = project_root / "src" / "llm_eval_control_plane" / "domain"
    forbidden_roots = {
        "fastapi",
        "httpx",
        "opentelemetry",
        "redis",
        "sqlalchemy",
        "typer",
    }

    violations: list[str] = []
    for source_path in sorted(domain_root.glob("*.py")):
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            else:
                continue

            for module in modules:
                root = module.split(".", maxsplit=1)[0]
                if root in forbidden_roots:
                    violations.append(f"{source_path.name}: imports {module}")
                if module.startswith(
                    "llm_eval_control_plane."
                ) and not module.startswith("llm_eval_control_plane.domain"):
                    violations.append(
                        f"{source_path.name}: imports outer layer {module}"
                    )

    assert violations == []
