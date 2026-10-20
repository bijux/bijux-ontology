"""Recognise relevant unsupported OWL expressions without cross-node effects."""

from rdflib import BNode, Graph, Namespace
from rdflib.namespace import OWL

from bijux_ontology.owl.encoding.constructs import unsupported_constructs

EX = Namespace("https://example.org/")


def test_relevant_constructs_are_distinguished_from_unrelated_expressions() -> None:
    graph = Graph()
    graph.add((EX.Datatype, OWL.datatypeComplementOf, EX.Other))
    graph.add((EX.Class, OWL.disjointUnionOf, BNode()))
    assert unsupported_constructs(graph, EX.Datatype) == (
        str(OWL.datatypeComplementOf),
    )
    assert unsupported_constructs(graph, EX.Class) == (str(OWL.disjointUnionOf),)
    assert unsupported_constructs(graph, EX.Independent) == ()
