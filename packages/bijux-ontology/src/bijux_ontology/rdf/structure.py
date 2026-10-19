"""Bounded RDF-list decoding without interpreting member semantics."""

from collections.abc import Iterator
from itertools import islice

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from ..errors import InputError, LimitExceeded
from ..limits import Budget


def read_rdf_list(
    graph: Graph, head: Node, *, max_items: int = 10_000
) -> tuple[Node, ...]:
    """Decode a finite, unbranched RDF list without deciding its member semantics.

    Empty lists and shared tails are legal. A requesting rule owns any minimum
    arity, named-class or literal-member requirement. No graph is mutated.
    """
    return tuple(iter_rdf_list(graph, head, max_items=max_items))


def iter_rdf_list(
    graph: Graph,
    head: Node,
    *,
    max_items: int = 10_000,
    budget: Budget | None = None,
) -> Iterator[Node]:
    """Yield proved positions, retaining a prefix before malformed or bounded tails.

    The current cell must have one first and one rest before its member is
    exposed. A caller may share its operation budget with list traversal.
    """
    if type(max_items) is not int or max_items <= 0:
        raise InputError("max_items must be a positive integer")
    seen: set[Node] = set()
    count = 0
    current = head
    while current != RDF.nil:
        if not isinstance(current, URIRef | BNode):
            raise InputError("RDF list link must be an IRI or blank node")
        if current in seen:
            raise InputError("RDF list contains a cycle")
        if count >= max_items:
            raise LimitExceeded("RDF list exceeds max_items")
        if budget is not None:
            budget.consume("work")
            budget.consume("states")
        seen.add(current)
        first = tuple(islice(graph.objects(current, RDF.first), 2))
        rest = tuple(islice(graph.objects(current, RDF.rest), 2))
        if len(first) != 1:
            raise InputError("RDF list requires exactly one rdf:first value")
        if len(rest) != 1:
            raise InputError("RDF list requires exactly one rdf:rest link")
        count += 1
        yield first[0]
        current = rest[0]
