"""Focused declaration references and reserved vocabulary lint."""

from rdflib import BNode, Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.vocabulary.checking import check_vocabulary

EX = Namespace("https://example.org/vocabulary#")


def test_named_references_require_kinds_and_allow_anonymous_expressions() -> None:
    graph = Graph().add((EX.Image, RDF.type, RDFS.Class))
    graph.add((EX.Image, RDFS.subClassOf, EX.missing))
    graph.add((EX.Image, RDFS.subClassOf, BNode()))
    graph.add((EX.name, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.name, RDFS.domain, EX.Image))
    graph.add((EX.name, RDFS.range, XSD.string))
    graph.add((EX.name, RDFS.subPropertyOf, EX.Image))
    report = check_vocabulary(graph, [], check_references=True)
    assert {(issue.code, issue.actual) for issue in report.issues} == {
        ("vocabulary.reference_missing", str(EX.missing)),
        ("vocabulary.reference_kind_mismatch", str(EX.Image)),
    }


def test_reserved_typo_and_object_datatype_conflict_are_explicit_opt_in_findings() -> (
    None
):
    graph = Graph().add((EX.Image, URIRef(str(OWL) + "subClassOf"), EX.Other))
    graph.add((EX.id, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.id, RDF.type, OWL.InverseFunctionalProperty))
    report = check_vocabulary(graph, [], lint=True, regime="owl-dl")
    assert {issue.code for issue in report.issues} == {
        "vocabulary.unknown_reserved_term",
        "vocabulary.declaration_conflict",
    }


def test_general_property_kind_accepts_specific_declarations_without_erasing_kind() -> (
    None
):
    graph = Graph()
    for predicate, kind in (
        (EX.image, OWL.ObjectProperty),
        (EX.name, OWL.DatatypeProperty),
        (EX.label, OWL.AnnotationProperty),
    ):
        graph.add((predicate, RDF.type, kind))
    assert check_vocabulary(
        graph,
        [
            TermDeclaration(str(predicate), (str(RDF.Property),))
            for predicate in (EX.image, EX.name, EX.label)
        ],
    ).verified
    assert (
        check_vocabulary(
            graph, [TermDeclaration(str(EX.name), (str(OWL.ObjectProperty),))]
        ).exit_code()
        == 1
    )
