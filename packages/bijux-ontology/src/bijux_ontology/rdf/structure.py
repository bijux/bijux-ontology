"""Bounded RDF-list decoding without interpreting member semantics."""

from itertools import islice

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from ..errors import InputError, LimitExceeded


def read_rdf_list(
    graph: Graph, head: Node, *, max_items: int = 10_000
) -> tuple[Node, ...]:
    """Decode a finite, unbranched RDF list without deciding its member semantics.

    Empty lists and shared tails are legal. A requesting rule owns any minimum
    arity, named-class or literal-member requirement. No graph is mutated.
    """
    if type(max_items) is not int or max_items <= 0:
        raise InputError("max_items must be a positive integer")
    seen: set[Node] = set()
    members: list[Node] = []
    current = head
    while current != RDF.nil:
        if not isinstance(current, URIRef | BNode):
            raise InputError("RDF list link must be an IRI or blank node")
        if current in seen:
            raise InputError("RDF list contains a cycle")
        if len(members) >= max_items:
            raise LimitExceeded("RDF list exceeds max_items")
        seen.add(current)
        first = tuple(islice(graph.objects(current, RDF.first), 2))
        rest = tuple(islice(graph.objects(current, RDF.rest), 2))
        if len(first) != 1:
            raise InputError("RDF list requires exactly one rdf:first value")
        if len(rest) != 1:
            raise InputError("RDF list requires exactly one rdf:rest link")
        members.append(first[0])
        current = rest[0]
    return tuple(members)
