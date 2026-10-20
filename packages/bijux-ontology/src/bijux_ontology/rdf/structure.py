"""Bounded RDF structural traversal without interpreting domain semantics."""

from collections.abc import Collection, Hashable, Iterable, Iterator, Mapping
from itertools import islice
from typing import TypeVar, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget

from .terms import bound_node, term_key


def _list_slot(
    graph: Graph, cell: Node, predicate: URIRef, budget: Budget | None
) -> tuple[Node, ...]:
    """Count exact RDF terms without confusing string spellings with branches."""
    raw = tuple(islice(graph.objects(cell, predicate), 3))
    if len(raw) < 2:
        return raw
    operation = budget if budget is not None else Budget()
    operation.consume("states", len(raw))
    operation.consume("bytes", 64 + 128 * len(raw))
    distinct: dict[tuple[str | None, ...], Node] = {}
    for node in raw:
        bound_node(node, operation)
        size = len(cast(str, node))
        if isinstance(node, Literal):
            size += len(node.datatype or "") + len(node.language or "")
        operation.consume("work", 4 * size + 1)
        operation.consume("bytes", 128 + 4 * size)
        distinct.setdefault(term_key(node), node)
    if len(raw) == 3 and len(distinct) == 1:
        raise LimitExceeded("RDF list slot uniqueness exceeds bounded observation")
    return tuple(distinct.values())


def read_rdf_list(
    graph: Graph, head: Node, *, max_items: int = 10_000, budget: Budget | None = None
) -> tuple[Node, ...]:
    """Decode a finite, unbranched RDF list without deciding its member semantics.

    Empty lists and shared tails are legal. A requesting rule owns any minimum
    arity, named-class or literal-member requirement. No graph is mutated.
    """
    return tuple(iter_rdf_list(graph, head, max_items=max_items, budget=budget))


def iter_rdf_list(
    graph: Graph,
    head: Node,
    *,
    max_items: int = 10_000,
    budget: Budget | None = None,
    constant_space: bool = False,
) -> Iterator[Node]:
    """Yield proved positions, retaining a prefix before malformed or bounded tails.

    The current cell must have one first and one rest before its member is
    exposed. A caller may share its operation budget with list traversal.
    Constant-space cycle detection retains two cells instead of a visited set;
    consumers using it must qualify completion before accepting streamed members.
    """
    if type(max_items) is not int or max_items <= 0:
        raise InputError("max_items must be a positive integer")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("RDF list traversal requires Budget")
    if type(constant_space) is not bool:
        raise InputError("Constant-space traversal must be boolean")
    seen: set[Node] | None = None if constant_space else set()
    count = 0
    current = head
    checkpoint = head
    power, distance = 1, 0
    while current != RDF.nil:
        if not isinstance(current, URIRef | BNode):
            raise InputError("RDF list link must be an IRI or blank node")
        if (seen is not None and current in seen) or (
            constant_space and count and current == checkpoint
        ):
            raise InputError("RDF list contains a cycle")
        if count >= max_items:
            raise LimitExceeded("RDF list exceeds max_items")
        if budget is not None:
            budget.consume("work")
            if seen is not None:
                budget.consume("states")
        if seen is not None:
            seen.add(current)
        elif distance == power:
            checkpoint = current
            power *= 2
            distance = 0
        first = _list_slot(graph, current, RDF.first, budget)
        rest = _list_slot(graph, current, RDF.rest, budget)
        if len(first) != 1:
            raise InputError("RDF list requires exactly one rdf:first value")
        if len(rest) != 1:
            raise InputError("RDF list requires exactly one rdf:rest link")
        count += 1
        yield first[0]
        current = rest[0]
        distance += 1


_Vertex = TypeVar("_Vertex", bound=Hashable)


def strong_components(
    nodes: Collection[_Vertex],
    forward: Mapping[_Vertex, Iterable[_Vertex]],
    backward: Mapping[_Vertex, Iterable[_Vertex]],
    *,
    budget: Budget,
    max_depth: int | None,
) -> Iterator[list[_Vertex]]:
    """Yield completed directed components over the caller's qualified identities.

    Adjacency and physical scope belong to the caller. Iterative traversal owns
    its bounded workspace; callers choose whether path depth has semantic force.
    A yielded group is complete even if later work exhausts the allowance.
    """
    if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
        raise InputError("Directed traversal depth must be nonnegative or absent")
    budget.reserve_storage(384)
    seen: set[_Vertex] = set()
    finished: list[_Vertex] = []
    for seed in nodes:
        budget.consume("work")
        if seed in seen:
            continue
        budget.consume("states")
        budget.reserve_storage(96)
        stack = [(seed, False, 0)]
        while stack:
            node, exiting, depth = stack.pop()
            budget.consume("work")
            if exiting:
                budget.consume("states")
                budget.reserve_storage(16)
                finished.append(node)
                continue
            if node in seen:
                continue
            if max_depth is not None and depth > max_depth:
                raise LimitExceeded("Directed graph depth exceeds max_depth")
            budget.consume("states", 2)
            budget.reserve_storage(160)
            seen.add(node)
            stack.append((node, True, depth))
            for child in forward[node]:
                budget.consume("work")
                if child not in seen:
                    budget.consume("states")
                    budget.reserve_storage(96)
                    stack.append((child, False, depth + 1))
    seen.clear()
    for seed in reversed(finished):
        budget.consume("work")
        if seed in seen:
            continue
        budget.consume("states")
        budget.reserve_storage(160)
        group: list[_Vertex] = []
        pending = [(seed, 0)]
        while pending:
            node, depth = pending.pop()
            budget.consume("work")
            if node in seen:
                continue
            if max_depth is not None and depth > max_depth:
                raise LimitExceeded("Directed graph depth exceeds max_depth")
            budget.consume("states", 2)
            budget.reserve_storage(80)
            seen.add(node)
            group.append(node)
            for parent in backward[node]:
                budget.consume("work")
                if parent not in seen:
                    budget.consume("states")
                    budget.reserve_storage(96)
                    pending.append((parent, depth + 1))
        yield group
