"""Installed command routing preserves native ShEx results and local resources."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def run(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the ordinary installed entry point without source-path injection."""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-m", "bijux_ontology", "shex", *args],
        cwd=directory,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def resources(directory: Path) -> None:
    """Supply an independently authored local schema and explicit import."""
    declaration = {
        "type": "ShapeDecl",
        "id": "urn:cli:shape",
        "shapeExpr": {
            "type": "Shape",
            "expression": {
                "type": "TripleConstraint",
                "predicate": "urn:cli:name",
                "valueExpr": {"type": "NodeConstraint", "nodeKind": "literal"},
            },
        },
    }
    context = "http://www.w3.org/ns/shex.jsonld"
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "@context": context,
                "type": "Schema",
                "shapes": [],
                "imports": ["urn:cli:child"],
            }
        )
    )
    (directory / "child.json").write_text(
        json.dumps(
            {
                "@context": context,
                "type": "Schema",
                "shapes": [declaration],
            }
        )
    )
    (directory / "data.ttl").write_text('<urn:cli:record> <urn:cli:name> "actual" .')


ARGS = (
    "data.ttl",
    "schema.json",
    "--schema-iri",
    "urn:cli:schema",
    "--binding",
    "urn:cli:record",
    "urn:cli:shape",
    "--json",
    "--strict",
)


def test_native_command_success_and_changed_required_property(tmp_path: Path) -> None:
    pytest.importorskip("pyrudof")
    resources(tmp_path)
    selected = (
        *ARGS,
        "--schema-import",
        "urn:cli:child",
        "child.json",
        "--timeout",
        "10",
    )
    positive = run(tmp_path, *selected)
    assert positive.returncode == 0, positive.stderr + positive.stdout
    data = json.loads(positive.stdout)
    assert data["verified"] and data["data"]["backend_version"] == "pyrudof 0.3.20"
    entry = data["data"]["obligations"][0]
    assert entry["status"] == "matched" and entry["native"]["status"] == "conformant"
    assert entry["native"]["node"] == "urn:cli:record"
    (tmp_path / "data.ttl").write_text('<urn:cli:record> <urn:cli:other> "actual" .')
    negative = run(tmp_path, *selected)
    assert negative.returncode == 1
    changed = json.loads(negative.stdout)
    assert not changed["verified"]
    entry = changed["data"]["obligations"][0]
    assert (
        entry["status"] == "not_matched"
        and entry["native"]["status"] == "nonconformant"
    )


def test_missing_import_and_import_output_alias_stay_safe(tmp_path: Path) -> None:
    resources(tmp_path)
    missing = run(tmp_path, *ARGS)
    assert missing.returncode != 0 and not json.loads(missing.stdout)["verified"]
    before = (tmp_path / "child.json").read_bytes()
    alias = run(
        tmp_path,
        *ARGS,
        "--schema-import",
        "urn:cli:child",
        "child.json",
        "--output",
        "child.json",
    )
    assert alias.returncode == 2 and (tmp_path / "child.json").read_bytes() == before
    assert "input.output_alias" in alias.stdout
