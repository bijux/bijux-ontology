"""Actual RDF sources define membership independently of application terms."""

from pathlib import Path

import pytest
from rdflib import BNode, Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/vocabulary#")


def test_membership_coverage_and_local_scope_do_not_accept_lookup_substitutes() -> None:
    local = Graph().add((EX.Image, RDF.type, RDFS.Class))
    local.add((EX.voxelSizeX, RDF.type, OWL.DatatypeProperty))
    lookup = Graph().add((EX.missing, RDF.type, OWL.Class))
    vocabulary = load_vocabulary(local, lookup=[lookup])
    report = check_vocabulary(
        vocabulary,
        [
            TermDeclaration(str(EX.Image), (str(OWL.Class),)),
            TermDeclaration(str(EX.missing)),
        ],
        local_namespaces=[str(EX)],
        require_coverage=True,
    )
    assert {(issue.code, issue.actual) for issue in report.issues} == {
        ("vocabulary.term_missing", str(EX.missing)),
        ("vocabulary.coverage_gap", str(EX.voxelSizeX)),
    }
    assert check_vocabulary(local, [TermDeclaration(str(EX.Image))]).verified
    assert len(local) == 2
    assert len(lookup) == 1


def test_kind_and_exact_iri_are_checked_without_rejecting_legal_class_punning() -> None:
    graph = Graph().add((EX.Image, RDF.type, OWL.Class))
    graph.add((EX.Image, RDF.type, OWL.NamedIndividual))
    report = check_vocabulary(
        graph,
        [
            TermDeclaration(str(EX.Image), (str(OWL.ObjectProperty),)),
            TermDeclaration(str(EX.Image).replace("https:", "http:")),
        ],
    )
    assert [issue.code for issue in report.issues] == [
        "vocabulary.entity_kind_mismatch",
        "vocabulary.term_missing",
    ]


def test_file_snapshot_retains_logical_base_and_imports_are_explicit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ontology.ttl"
    path.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#>.\n"
        "<> owl:imports <import.ttl>. <#Image> a owl:Class."
    )
    imported = tmp_path / "external.ttl"
    imported.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#>.\n"
        "<> owl:imports <ontology.ttl>. <#Other> a owl:Class."
    )
    base = "https://example.org/models/ontology.ttl"
    with pytest.raises(InputError) as error:
        load_vocabulary(path, base=base)
    assert "Required import" in str(error.value)
    vocabulary = load_vocabulary(
        path,
        base=base,
        imports={
            "https://example.org/models/import.ttl": imported,
        },
    )
    assert check_vocabulary(vocabulary, [TermDeclaration(base + "#Image")]).verified
    assert len(vocabulary.imports) == 1
    with pytest.raises(InputError) as missing:
        load_vocabulary(tmp_path / "missing.ttl")
    assert "Cannot read RDF source" in str(missing.value)
    malformed = tmp_path / "malformed.nt"
    malformed.write_text("not valid RDF")
    with pytest.raises(InputError) as invalid:
        load_vocabulary(malformed)
    assert "Cannot parse nt RDF source" in str(invalid.value)


def test_independent_blank_nodes_and_input_graphs_remain_separate() -> None:
    identifier = BNode("same-source-label")
    first = Graph().add((EX.Image, RDFS.subClassOf, identifier))
    second = Graph().add((EX.Other, RDFS.subClassOf, identifier))
    vocabulary = load_vocabulary(first, lookup=[second])
    assert len(set(vocabulary.graph().objects(None, RDFS.subClassOf))) == 2
    assert set(first.objects(None, RDFS.subClassOf)) == {identifier}


def test_empty_or_unspecified_mandatory_scope_does_not_pass() -> None:
    assert check_vocabulary(Graph(), []).exit_code() == 2
    optional = check_vocabulary(Graph(), [], allow_empty=True)
    assert optional.exit_code() == 0
    assert optional.outcome == "not_applicable"
    assert not optional.verified
    assert check_vocabulary(Graph(), [], require_coverage=True).exit_code() == 2
