"""Keep library features independent of command execution and optional adapters."""

import ast
import graphlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

FOUNDATION = {
    "execution",
    "absence",
    "diagnostics",
    "json",
}
PACKAGES = {
    "rdf",
    "jsonld",
    "owl",
    "declarations",
    "documents",
    "vocabulary",
    "models",
    "skos",
    "shacl",
    "shex",
    "data",
    "interfaces.cli",
    "interfaces.execution",
    "interfaces.exports",
    "interfaces.adapters",
    "workflows.projects",
    "workflows.ontology",
    "workflows.upgrades",
    "workflows.reproductions",
    "workflows.revisions",
    "workflows.corpus",
    "workflows.finding_streams",
}


def _owner(module: str) -> str:
    parts = module.removeprefix("bijux_ontology.").split(".")
    if parts[0] in {"interfaces", "workflows"} and len(parts) > 1:
        return ".".join(parts[:2])
    return "interfaces.cli" if parts[0] == "__main__" else parts[0]


def _within(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".") for prefix in prefixes
    )


@pytest.fixture(scope="module")
def dependencies() -> dict[str, set[str]]:
    root = Path(__file__).resolve().parents[3] / "src" / "bijux_ontology"
    modules = {
        ".".join(path.relative_to(root.parent).with_suffix("").parts).removesuffix(
            ".__init__"
        ): path
        for path in root.rglob("*.py")
    }
    edges: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        parent = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                target = (
                    importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), parent
                    )
                    if node.level
                    else node.module or ""
                )
                if target in modules and any(
                    target + "." + alias.name not in modules for alias in node.names
                ):
                    edges[name].add(target)
                edges[name].update(
                    target + "." + alias.name
                    for alias in node.names
                    if target + "." + alias.name in modules
                )
            elif isinstance(node, ast.Import):
                edges[name].update(
                    alias.name for alias in node.names if alias.name in modules
                )
    return edges


def test_internal_imports_are_acyclic(dependencies: dict[str, set[str]]) -> None:
    # Include deferred imports: they must not hide a dependency cycle.
    try:
        tuple(graphlib.TopologicalSorter(dependencies).static_order())
    except graphlib.CycleError as error:
        pytest.fail(f"Internal import cycle: {error.args[1]}")


@pytest.fixture(scope="module")
def facades(dependencies: dict[str, set[str]]) -> set[str]:
    root = Path(__file__).resolve().parents[3] / "src"
    result = set()
    for module, targets in dependencies.items():
        package = root.joinpath(*module.split("."), "__init__.py")
        if not targets or not (package.exists()):
            continue
        source = (
            package
            if package.exists()
            else root.joinpath(*module.split(".")).with_suffix(".py")
        )
        for node in ast.parse(source.read_text(encoding="utf-8")).body:
            if module in {"bijux_ontology", "bijux_ontology.shacl"}:
                if isinstance(node, ast.If):
                    assert (
                        isinstance(node.test, ast.Name)
                        and node.test.id == "TYPE_CHECKING"
                    )
                    assert not node.orelse
                    assert all(isinstance(item, ast.ImportFrom) for item in node.body)
                    continue
                if isinstance(node, ast.FunctionDef):
                    assert node.name in {"__getattr__", "__dir__"}
                    continue
                if (
                    module == "bijux_ontology.shacl"
                    and isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "_EXPORT_MODULES"
                        for target in node.targets
                    )
                ):
                    assert isinstance(node.value, ast.Dict)
                    assert all(
                        isinstance(item, ast.Constant) and isinstance(item.value, str)
                        for item in (*node.value.keys, *node.value.values)
                    )
                    continue
            assert (
                isinstance(node, (ast.Import, ast.ImportFrom))
                or (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
                or (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, (ast.List, ast.Tuple))
                    and all(
                        isinstance(item, ast.Constant) and isinstance(item.value, str)
                        for item in node.value.elts
                    )
                    and all(
                        isinstance(target, ast.Name) and target.id == "__all__"
                        for target in node.targets
                    )
                )
            ), f"Public facade contains executable behavior: {module}"
        result.add(module)
    return result


def test_owned_packages_are_acyclic(
    dependencies: dict[str, set[str]], facades: set[str]
) -> None:
    # Public reexports aggregate APIs; concrete engines may not import through them.
    packages: dict[str, set[str]] = {}
    for module, targets in dependencies.items():
        if module in facades:
            continue
        owner = _owner(module)
        packages.setdefault(owner, set())
        packages[owner].update(
            _owner(target) for target in targets if _owner(target) != owner
        )
    try:
        tuple(graphlib.TopologicalSorter(packages).static_order())
    except graphlib.CycleError as error:
        pytest.fail(f"Owned package dependency cycle: {error.args[1]}")


def test_library_dependencies_follow_feature_ownership(
    dependencies: dict[str, set[str]], facades: set[str]
) -> None:
    namespaces = {
        "bijux_ontology",
        "bijux_ontology.interfaces",
        "bijux_ontology.workflows",
    }
    for module, targets in dependencies.items():
        owner = _owner(module)
        assert owner in FOUNDATION | PACKAGES or module in namespaces, (
            f"Unowned module: {module}"
        )
        if owner != "interfaces.adapters":
            assert all(_owner(target) != "interfaces.adapters" for target in targets), (
                f"Core import of optional adapters: {module}"
            )
        if module in facades:
            continue
        for target in targets:
            # Convenience exports must not launder reverse engine dependencies.
            assert target not in facades or (
                module == "bijux_ontology.__main__"
                and target == "bijux_ontology.interfaces.cli"
            ), f"{module} -> facade {target}"
            target_owner = _owner(target)
            if owner in FOUNDATION:
                assert target_owner in FOUNDATION, f"{module} -> {target}"
            if module == "bijux_ontology.json.paths":
                assert target_owner in FOUNDATION, f"{module} -> {target}"
            if owner == "rdf":
                assert target_owner in FOUNDATION | {"rdf"}, f"{module} -> {target}"
            if owner in {"jsonld", "owl", "declarations", "shex"}:
                assert target_owner in FOUNDATION | {"rdf", owner}, (
                    f"{module} -> {target}"
                )
            if not owner.startswith("interfaces."):
                assert not target_owner.startswith("interfaces."), (
                    f"{module} -> {target}"
                )
                if not owner.startswith("workflows."):
                    assert not target_owner.startswith("workflows."), (
                        f"{module} -> {target}"
                    )
        forbidden = []
        if module in {
            "bijux_ontology.interfaces.cli.inputs",
            "bijux_ontology.diagnostics.reports",
            "bijux_ontology.interfaces.cli.vocabularies",
            "bijux_ontology.interfaces.cli.transforms",
        }:
            forbidden.append("bijux_ontology.workflows.projects")
        if module == "bijux_ontology.interfaces.cli.arguments":
            forbidden.append("bijux_ontology.interfaces.cli")
        if module in {
            "bijux_ontology.jsonld.observations",
            "bijux_ontology.jsonld.expectations",
        }:
            forbidden.extend(
                "bijux_ontology.jsonld." + name
                for name in (
                    "resources",
                    "selection",
                    "expansion",
                    "checking",
                    "conversion",
                    "preparation",
                )
            )
        if module in {
            "bijux_ontology.workflows.projects.configuration",
            "bijux_ontology.workflows.projects.jobs",
            "bijux_ontology.workflows.projects.resources",
        }:
            forbidden.extend(
                (
                    "bijux_ontology.workflows.projects.checking",
                    "bijux_ontology.workflows.projects.operations",
                )
            )
        if module in {
            "bijux_ontology.workflows.projects.configuration",
            "bijux_ontology.workflows.projects.jobs",
        }:
            forbidden.append("bijux_ontology.workflows.projects.resources")
        for target in targets:
            assert not _within(target, tuple(forbidden)), f"{module} -> {target}"


def test_narrow_imports_load_only_explicit_owners() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            """
import sys
import bijux_ontology
import bijux_ontology.shacl
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import own_json
assert not any(name.startswith("rdflib") for name in sys.modules)
assert "bijux_ontology.interfaces.api" not in sys.modules
assert "bijux_ontology.models.contracts.checking" not in sys.modules
assert "bijux_ontology.shacl.checking" not in sys.modules
assert "__getattr__" not in vars(bijux_ontology)
assert "__getattr__" not in vars(bijux_ontology.shacl)
from bijux_ontology.diagnostics.errors import InputError
assert issubclass(InputError, Exception)
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.browsing.pages import terms_page
assert all(callable(item) for item in (load_vocabulary, inspect_vocabulary, terms_page))
""",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
