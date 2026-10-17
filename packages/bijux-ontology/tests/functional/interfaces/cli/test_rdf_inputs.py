"""Per-version RDF selections preserve resource identity and input protection."""

import json
from pathlib import Path

import pytest

from bijux_ontology.interfaces.cli import main


def test_upgrade_resolves_each_versions_syntax_base_and_resources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    iri = "https://example.org/"
    before = tmp_path / "before.snapshot"
    before.write_text(
        "<../Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    after = tmp_path / "after.snapshot"
    after.write_text(
        json.dumps({"@context": iri + "schema", "@id": "../Record", "@type": "Class"}),
        encoding="utf-8",
    )
    context = tmp_path / "context.json"
    context_text = json.dumps(
        {"@context": {"Class": "http://www.w3.org/2002/07/owl#Class"}}
    )
    context.write_text(context_text, encoding="utf-8")
    resources = tmp_path / "resources.json"
    resources.write_text(json.dumps({iri + "schema": "context.json"}), encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([iri + "Record"]), encoding="utf-8")
    args = [
        "upgrade",
        str(before),
        str(after),
        "--inventory",
        str(inventory),
        "--format",
        "turtle",
        "--base",
        iri + "before/",
        "--after-format",
        "json-ld",
        "--after-base",
        iri + "after/",
        "--after-sources",
        str(resources),
        "--json",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] and result["data"]["contract_verdict"] == "agreement"
    assert not result["data"]["changes"]
    assert main(args + ["--output", str(context)]) == 2
    assert context.read_text(encoding="utf-8") == context_text
    assert (
        json.loads(capsys.readouterr().out)["issues"][0]["code"] == "input.output_alias"
    )
