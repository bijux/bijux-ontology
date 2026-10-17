"""Ontology composition preserves references, scope and publication policy."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.vocabulary import Vocabulary
from bijux_ontology.workflows.ontology.checking import check_ontology

EX = Namespace("https://example.org/")


def test_structural_references_separate_malformed_missing_and_anonymous() -> None:
    graph = Graph()
    graph.add((EX.Class, RDF.type, OWL.Class))
    graph.add((EX.Class, RDFS.subClassOf, Literal("not a class")))
    graph.add((EX.Class, OWL.equivalentClass, EX.Missing))
    graph.add((EX.Class, OWL.disjointWith, BNode()))
    graph.add((EX.Class, EX.citation, EX.UnrelatedReference))
    report = check_ontology(graph, families=("references",))
    assert {issue.code for issue in report.issues} == {
        "ontology.reference_shape",
        "ontology.reference_missing",
        "ontology.expression_unverified",
    }
    assert all(issue.actual != str(EX.UnrelatedReference) for issue in report.issues)
    assert not report.verified


def test_documentation_and_deprecation_are_opt_in_and_never_rewrite() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.A, RDFS.label, Literal("A", lang="EN")))
    graph.add((EX.A, OWL.deprecated, Literal(True)))
    graph.add((EX.A, EX.replacedBy, EX.B))
    assert check_ontology(graph).verified
    report = check_ontology(
        graph,
        families=("metadata", "deprecated"),
        required_annotations=(str(RDFS.label),),
        languages=("en",),
        used_terms=(str(EX.A),),
        replacement_predicates=(str(EX.replacedBy),),
    )
    assert [issue.code for issue in report.issues] == [
        "ontology.deprecated_term",
        "ontology.replacement_unresolved",
    ]
    assert report.issues[0].actual == str(EX.B)
    assert not report.verified
    assert (EX.A, OWL.deprecated, Literal(True)) in graph


def test_empty_and_bounded_selected_checks_cannot_manufacture_agreement() -> None:
    assert not check_ontology(Graph(), families=("references",)).verified
    with pytest.raises(InputError):
        check_ontology(Graph(), families=())
    graph = Graph().add((EX.A, RDFS.subClassOf, Literal("bad")))
    exhausted = check_ontology(
        graph, families=("references",), limits=Limits(max_issues=1, max_work=1)
    )
    assert exhausted.outcome == "exhausted"
    assert not exhausted.complete and not exhausted.verified
    assert exhausted.issues[0].code == "ontology.reference_missing"
    assert exhausted.issues[-1].kind == "exhausted"


def test_reserved_checks_do_not_reinterpret_import_metadata_or_skip_datatypes() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.A, OWL.imports, EX.descriptiveLookup))
    graph.add(
        (EX.A, EX.value, Literal("value", datatype=URIRef(str(XSD) + "typoDatatype")))
    )
    view = Vocabulary(graph)
    report = check_ontology(view, families=("reserved",))
    assert [issue.code for issue in report.issues] == [
        "vocabulary.unknown_reserved_term"
    ]
    assert report.issues[0].actual == str(URIRef(str(XSD) + "typoDatatype"))
