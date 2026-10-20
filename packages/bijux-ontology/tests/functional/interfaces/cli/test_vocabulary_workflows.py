"""CLI workflows share real SKOS checks and copied RDF artifact semantics."""

import json
from pathlib import Path

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.interfaces.cli import main

EX = "https://example.org/"


def test_skos_cli_and_projects_keep_profile_and_hierarchy_choices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from bijux_ontology.workflows.projects.checking import check_project
    from bijux_ontology.workflows.projects.configuration import ProjectConfig

    vocabulary = tmp_path / "terms.ttl"
    vocabulary.write_text(
        "@prefix skos:<http://www.w3.org/2004/02/skos/core#>. "
        "<urn:a> skos:inScheme <urn:one>; skos:broadMatch <urn:b>. "
        "<urn:b> skos:inScheme <urn:two>.",
        encoding="utf-8",
    )
    for basis, status in (("asserted", 0), ("effective", 1)):
        assert (
            main(
                [
                    "skos",
                    str(vocabulary),
                    "--rule",
                    "same-scheme-hierarchy",
                    "--hierarchy-basis",
                    basis,
                    "--json",
                ]
            )
            == status
        )
        report = json.loads(capsys.readouterr().out)
        assert "hierarchy-policy:" + basis in report["families"]
    vocabulary.write_text(
        "@prefix skos:<http://www.w3.org/2004/02/skos/core#>. "
        "<urn:a> skos:broadMatch <urn:b>. <urn:b> a skos:ConceptScheme.",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "skos",
                str(vocabulary),
                "--concept",
                "urn:a",
                "--profile",
                "mapping-integrity",
                "--json",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert "mapping-endpoints" in report["families"]
    assert any(
        issue["code"] == "skos.resource_role_conflict" for issue in report["issues"]
    )
    config = ProjectConfig(
        {
            "version": 1,
            "rdf": {"terms": {"path": "terms.ttl"}},
            "checks": [
                {
                    "name": "mapping",
                    "kind": "skos",
                    "source": "terms",
                    "concepts": ["urn:a"],
                    "profile": "mapping-integrity",
                    "hierarchy_basis": "asserted",
                }
            ],
        },
        directory=tmp_path,
    )
    checked = check_project(config)
    assert checked.outcome == "disagreement"
    assert any(issue.code == "skos.resource_role_conflict" for issue in checked.issues)


def test_skos_publication_policy_and_registered_code_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vocabulary = tmp_path / "colours.ttl"
    vocabulary.write_text(
        f"<{EX}red> a <http://www.w3.org/2004/02/skos/core#Concept>; "
        '<http://www.w3.org/2004/02/skos/core#prefLabel> "Red"@en .',
        encoding="utf-8",
    )
    assert main(["skos", str(vocabulary), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]
    assert (
        main(
            [
                "skos",
                str(vocabulary),
                "--rule",
                "language-coverage",
                "--language",
                "sv",
                "--json",
            ]
        )
        == 1
    )
    issue = json.loads(capsys.readouterr().out)["issues"][0]
    assert main(["explain", issue["code"], "--json"]) == 0
    explanation = json.loads(capsys.readouterr().out)["data"]
    assert explanation["code"] == issue["code"] and explanation["example"]
    assert main(["explain", "invented.reasoning_pass", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["outcome"] == "input_error"


@pytest.mark.parametrize("operation", ["extract", "remap", "merge", "convert"])
def test_transformations_write_new_verified_candidates_and_preserve_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    original = (
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class>; "
        '<http://www.w3.org/2000/01/rdf-schema#label> "Colour"@en-GB .'
    )
    source = tmp_path / "terms.ttl"
    source.write_text(original, encoding="utf-8")
    output = tmp_path / "candidate.nt"
    args = [
        "transform",
        str(source),
        "--operation",
        operation,
        "--destination",
        str(output),
        "--to-format",
        "nt",
        "--json",
    ]
    if operation == "extract":
        args += ["--term", EX + "Record", "--predicate", str(RDFS.label)]
    elif operation == "remap":
        mapping = tmp_path / "mapping.json"
        mapping.write_text(json.dumps({EX + "Record": EX + "Entry"}), encoding="utf-8")
        args += ["--mapping", str(mapping)]
    elif operation == "merge":
        other = tmp_path / "other.ttl"
        other.write_text(f'<{EX}Other> <{RDFS.label}> "Other" .', encoding="utf-8")
        args += ["--merge-source", str(other)]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["written"]
    graph = prepare_source(output).graph()
    subject = URIRef(EX + ("Entry" if operation == "remap" else "Record"))
    assert (subject, RDFS.label, Literal("Colour", lang="en-GB")) in graph
    if operation == "extract":
        assert (subject, RDF.type, None) not in graph
    if operation == "merge":
        assert (
            URIRef(EX + "Other"),
            RDFS.label,
            Literal("Other", datatype=XSD.string),
        ) in graph
    assert source.read_text(encoding="utf-8") == original
    candidate = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == candidate
    capsys.readouterr()


def test_jsonld_vocabulary_uses_explicit_context_snapshot_map(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "context.json").write_text(
        json.dumps({"@context": {"Class": "http://www.w3.org/2002/07/owl#Class"}}),
        encoding="utf-8",
    )
    source = tmp_path / "vocabulary.snapshot"
    source.write_text(
        json.dumps({"@context": EX + "context", "@id": "Record", "@type": "Class"}),
        encoding="utf-8",
    )
    resources = tmp_path / "resources.json"
    resources.write_text(json.dumps({EX + "context": "context.json"}), encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([EX + "Record"]), encoding="utf-8")
    args = [
        "vocabulary",
        str(source),
        str(inventory),
        "--format",
        "json-ld",
        "--base",
        EX,
        "--json",
    ]
    assert main(args + ["--sources", str(resources)]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]
    assert main(args) == 2
    assert not json.loads(capsys.readouterr().out)["verified"]


def test_upgrade_retains_affected_fields_and_incomplete_context_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tmp_path / "before.ttl"
    after = tmp_path / "after.ttl"
    ontology = (
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> . "
        f"<{EX}title> a <http://www.w3.org/2002/07/owl#DatatypeProperty> ."
    )
    before.write_text(ontology, encoding="utf-8")
    after.write_text(ontology, encoding="utf-8")
    declarations = tmp_path / "models.json"
    declarations.write_text(
        json.dumps(
            [
                {
                    "identifier": "record",
                    "types": [EX + "Record"],
                    "fields": [
                        {
                            "name": "title",
                            "predicate": EX + "title",
                            "values": [
                                {
                                    "kind": "literal",
                                    "datatype": str(XSD.string),
                                }
                            ],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    documents = [tmp_path / "before.json", tmp_path / "after.json"]
    for path in documents:
        path.write_text(
            json.dumps({"@context": {"title": EX + "title"}, "title": "Colour"}),
            encoding="utf-8",
        )
    expectations = tmp_path / "expectations.json"
    expectations.write_text(
        json.dumps([{"path": ["title"], "predicate": EX + "title"}]), encoding="utf-8"
    )
    args = [
        "upgrade",
        str(before),
        str(after),
        "--declarations",
        str(declarations),
        "--context-before",
        str(documents[0]),
        "--context-after",
        str(documents[1]),
        "--expectations",
        str(expectations),
        "--json",
    ]
    assert main(args) == 0
    stable = json.loads(capsys.readouterr().out)
    assert stable["data"]["contract_verdict"] == "agreement"
    after.write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    assert main(args) == 1
    changed = json.loads(capsys.readouterr().out)
    assert changed["data"]["contract_verdict"] == "demonstrated-break"
    assert any(item["use"]["field"] == "title" for item in changed["data"]["affected"])
    documents[1].unlink()
    destination = tmp_path / "existing-report.json"
    original = "previous verified report\n"
    destination.write_text(original, encoding="utf-8")
    for execution in ([], ["--timeout", "20"]):
        assert main(args + ["--output", str(destination)] + execution) == 2
        assert destination.read_text(encoding="utf-8") == original
        incomplete = json.loads(capsys.readouterr().out)
        assert incomplete["outcome"] == "input_error" and not incomplete["verified"]
        assert incomplete["data"]["contracts"]
