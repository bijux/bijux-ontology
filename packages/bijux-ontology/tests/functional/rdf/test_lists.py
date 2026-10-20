"""Finite structural decoding distinguishes malformed and unsupported input."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.structure import iter_rdf_list, read_rdf_list


def test_constant_cell_memory_preserves_order_and_rejects_long_cycles() -> None:
    """Streaming qualification can inspect a broad list under a small state cap."""
    graph = Graph()
    cells = tuple(BNode() for _ in range(1000))
    for index, cell in enumerate(cells):
        graph.add((cell, RDF.first, URIRef(f"urn:list:item:{index}")))
        graph.add((cell, RDF.rest, cells[index + 1] if index < 999 else RDF.nil))
    budget = Budget(Limits(max_states=10, max_work=5000))
    members = tuple(iter_rdf_list(graph, cells[0], budget=budget, constant_space=True))
    assert len(members) == 1000 and str(members[-1]) == "urn:list:item:999"
    assert budget.remaining("states") == 10
    graph.set((cells[-1], RDF.rest, cells[500]))
    with pytest.raises(InputError, match="cycle"):
        tuple(
            iter_rdf_list(
                graph,
                cells[0],
                budget=Budget(Limits(max_states=10, max_work=5000)),
                constant_space=True,
            )
        )
    with pytest.raises(LimitExceeded):
        tuple(iter_rdf_list(graph, cells[0], max_items=20, constant_space=True))


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


def test_string_aliases_preserve_the_actual_member_and_do_not_hide_branches() -> None:
    aliases = (Literal("same"), Literal("same", datatype=XSD.string))
    for order in (aliases, aliases[::-1]):
        graph = Graph()
        head = BNode()
        for member in order:
            graph.add((head, RDF.first, member))
        graph.add((head, RDF.rest, RDF.nil))
        original = next(graph.objects(head, RDF.first))
        assert read_rdf_list(graph, head)[0] is original
    for position in range(3):
        members = list(aliases)
        members.insert(position, Literal("different"))
        graph = Graph()
        head = BNode()
        for member in members:
            graph.add((head, RDF.first, member))
        graph.add((head, RDF.rest, RDF.nil))
        with pytest.raises(InputError, match="rdf:first"):
            read_rdf_list(graph, head)
    graph = Graph()
    head = BNode()
    for lexical in ("01", "1"):
        graph.add(
            (head, RDF.first, Literal(lexical, datatype=XSD.integer, normalize=False))
        )
    graph.add((head, RDF.rest, RDF.nil))
    with pytest.raises(InputError, match="rdf:first"):
        read_rdf_list(graph, head)


@pytest.mark.parametrize(
    "limits",
    (Limits(max_work=10), Limits(max_states=2), Limits(max_total_bytes=64)),
)
def test_late_string_alias_budget_cut_preserves_only_proved_positions(
    limits: Limits,
) -> None:
    graph = Graph()
    head, tail = BNode(), BNode()
    graph.add((head, RDF.first, EX.Proved))
    graph.add((head, RDF.rest, tail))
    for member in (Literal("same"), Literal("same", datatype=XSD.string)):
        graph.add((tail, RDF.first, member))
    graph.add((tail, RDF.rest, RDF.nil))
    result = iter_rdf_list(graph, head, budget=Budget(limits))
    assert next(result) == EX.Proved
    with pytest.raises(LimitExceeded):
        next(result)
