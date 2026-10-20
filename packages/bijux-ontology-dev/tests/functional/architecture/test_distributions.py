"""Optional distributions depend on core without reversing package ownership."""

import ast
from pathlib import Path


def test_distribution_imports_preserve_independent_core_ownership() -> None:
    root = Path(__file__).resolve().parents[5]
    owners = {
        "bijux_ontology": root / "packages/bijux-ontology/src",
        "bijux_ontology_adapters": root / "packages/bijux-ontology-adapters/src",
        "bijux_ontology_reasoning": root / "packages/bijux-ontology-reasoning/src",
    }
    edges: dict[str, set[str]] = {owner: set() for owner in owners}
    for owner, source in owners.items():
        for path in source.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    names.append(node.value)
                for name in names:
                    dependency = name.partition(".")[0]
                    if dependency in owners and dependency != owner:
                        edges[owner].add(dependency)
    assert edges["bijux_ontology"] == set(), edges
    assert edges["bijux_ontology_reasoning"] <= {"bijux_ontology"}, edges
    assert edges["bijux_ontology_adapters"] <= {"bijux_ontology"}, edges
