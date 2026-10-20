"""Installed core and adapter diagnostics retain complete literal-code guidance."""

import ast
from pathlib import Path

import bijux_ontology
import bijux_ontology_adapters
from bijux_ontology.interfaces.cli.diagnostics import explain_code


def test_literal_issue_codes_have_installed_guidance() -> None:
    roots = (
        Path(bijux_ontology.__file__).resolve().parent,
        Path(bijux_ontology_adapters.__file__).resolve().parent,
    )
    codes: set[str] = set()
    for source in (path for root in roots for path in root.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            first = node.args[0]
            if (
                name == "Issue"
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                codes.add(first.value)
    assert codes
    assert {code for code in codes if explain_code(code) is None} == set()
