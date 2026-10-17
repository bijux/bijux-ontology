"""Finite structural decoding distinguishes malformed and unsupported input."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.rdf.structure import read_rdf_list

EX = Namespace("https://example.org/")


def test_list_members_shared_tails_cycles_and_bounds() -> None:
    graph = Graph()
    tail = BNode()
    Collection(graph, tail, [EX.Class, Literal("literal member")])
    assert read_rdf_list(graph, RDF.nil) == ()
    assert read_rdf_list(graph, tail) == (EX.Class, Literal("literal member"))
    with pytest.raises(LimitExceeded):
        read_rdf_list(graph, tail, max_items=1)
    head = BNode()
    graph.add((head, RDF.first, EX.Other))
    graph.add((head, RDF.rest, tail))
    assert read_rdf_list(graph, head)[1:] == read_rdf_list(graph, tail)
    graph.set((head, RDF.rest, head))
    with pytest.raises(InputError, match="cycle"):
        read_rdf_list(graph, head)
    graph.add((head, RDF.first, EX.Unexpected))
    with pytest.raises(InputError, match="rdf:first"):
        read_rdf_list(graph, head)
