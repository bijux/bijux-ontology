"""Installed diagnostic guidance stays discoverable and honest about scope."""

import ast
import json
from pathlib import Path

import bijux_ontology
from bijux_ontology.interfaces.cli.diagnostics import explain_code


def test_missing_unsupported_and_incomplete_conditions_have_distinct_remedies() -> None:
    missing = explain_code("vocabulary.term_missing")
    unsupported = explain_code("models.expression_unverified")
    limited = explain_code("execution.limit_exceeded")
    cancelled = explain_code("execution.cancelled")
    assert missing is not None and unsupported is not None
    assert limited is not None and cancelled is not None
    assert "absent" in missing.meaning and "source selection" in missing.action
    assert "unverified" in unsupported.meaning
    assert "not interpret this as a proven conflict" in unsupported.action
    assert "allowance" in limited.meaning and "rerun" in limited.action
    assert "cancelled" in cancelled.meaning
    assert missing.to_dict()["code"] == "vocabulary.term_missing"
    assert limited.to_dict()["scope"] == "requested execution"
    context = explain_code("context.mapping_mismatch")
    assert context is not None
    assert json.loads(context.example) == {
        "@context": {"label": {"@id": "https://example.org/label"}}
    }
    assert explain_code("context.datatype_mismatch") is not None
    assert explain_code("models.contextual_datatype_conflict") is not None
    for status in (
        "shapes_invalid",
        "unavailable",
        "failure",
        "input_error",
        "exhausted",
    ):
        assert explain_code("shacl." + status) is not None
    introduced = explain_code("upgrade.contract_introduced")
    persistent = explain_code("upgrade.contract_persistent")
    assert introduced is not None and persistent is not None
    assert introduced.meaning != persistent.meaning
    assert explain_code("context.invented_condition") is None
    assert explain_code("invented.limit_exceeded") is None
    assert explain_code("") is None


def test_literal_issue_codes_have_installed_guidance() -> None:
    root = Path(bijux_ontology.__file__).resolve().parent
    codes: set[str] = set()
    for source in root.rglob("*.py"):
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
