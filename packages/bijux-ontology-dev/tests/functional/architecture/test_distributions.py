"""Optional distributions depend on core without reversing package ownership."""

import ast
import graphlib
import importlib.util
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
        package = source / owner
        assert package.is_dir(), f"Missing distribution source: {package}"
        modules = {
            ".".join(path.relative_to(source).with_suffix("").parts).removesuffix(
                ".__init__"
            ): path
            for path in package.rglob("*.py")
        }
        assert modules, f"Empty distribution source: {package}"
        internal: dict[str, set[str]] = {module: set() for module in modules}
        for module, path in modules.items():
            parent = module if path.name == "__init__.py" else module.rpartition(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                    internal[module].update(
                        alias.name for alias in node.names if alias.name in modules
                    )
                elif isinstance(node, ast.ImportFrom):
                    target = (
                        importlib.util.resolve_name(
                            "." * node.level + (node.module or ""), parent
                        )
                        if node.level
                        else node.module or ""
                    )
                    names.append(target)
                    if target in modules and any(
                        target + "." + alias.name not in modules for alias in node.names
                    ):
                        internal[module].add(target)
                    internal[module].update(
                        target + "." + alias.name
                        for alias in node.names
                        if target + "." + alias.name in modules
                    )
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    names.append(node.value)
                for name in names:
                    dependency = name.partition(".")[0]
                    if dependency in owners and dependency != owner:
                        edges[owner].add(dependency)
        if owner == "bijux_ontology_adapters":
            # Deferred imports must not hide internal adapter dependency cycles.
            tuple(graphlib.TopologicalSorter(internal).static_order())
    assert edges["bijux_ontology"] == set(), edges
    assert edges["bijux_ontology_reasoning"] <= {"bijux_ontology"}, edges
    assert edges["bijux_ontology_adapters"] <= {"bijux_ontology"}, edges
