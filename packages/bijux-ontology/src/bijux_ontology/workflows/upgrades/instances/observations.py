"""Observe selected physical records through existing RDF path and list readers."""

from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.patches.planning import Captured
from bijux_ontology.rdf.paths.traversal import finite_path_steps, path_values
from bijux_ontology.rdf.paths.types import RdfPath
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import bound_node, literal_datatype

from .types import InstanceEntity

EntityKey = tuple[PatchResource, PatchResource | None]


@dataclass(frozen=True)
class SelectedEntity:
    """A validated actual subject and its graph-scoped exact reference."""

    original: InstanceEntity
    key: EntityKey
    graph: Graph


def reference(node: Node, captured: Captured, budget: Budget) -> PatchValue:
    """Retain exact lexical terms and use only the original capture's blank owner."""
    bound_node(node, budget)
    if isinstance(node, BNode):
        return captured.reference(node)
    if isinstance(node, URIRef):
        return node
    if isinstance(node, Literal):
        budget.consume("states")
        return PatchLiteral(str(node), str(literal_datatype(node)), node.language)
    raise InputError("Instance observations require ordinary RDF terms")


def select_entity(
    source: PreparedSource, captured: Captured, entity: InstanceEntity, budget: Budget
) -> SelectedEntity:
    """Require an actual selected subject; object-only and excluded-graph hits fail."""
    if not isinstance(entity.subject, URIRef | BNode):
        raise InputError("Instance entity subjects must be RDF resources")
    if captured.witness.kind == "dataset" and entity.graph is None:
        raise PatchUnavailable("Dataset entities require an explicit physical graph")
    graph = source._read(entity.graph, budget=budget)
    subject = cast(PatchResource, reference(entity.subject, captured, budget))
    scope = captured.witness.graph_selection
    if captured.witness.kind == "dataset":
        scope = (
            None
            if entity.graph == "@default"
            else cast(PatchResource, reference(graph.identifier, captured, budget))
        )
    budget.consume("work", len(captured.witness.quads))
    if not any(
        q.subject == subject and q.graph == scope for q in captured.witness.quads
    ):
        raise PatchUnavailable(
            "Selected entity has no subject occurrence in its physical graph"
        )
    return SelectedEntity(entity, (subject, scope), graph)


def record_support(
    captured: Captured, key: EntityKey, budget: Budget
) -> tuple[PatchQuad, ...]:
    """Protect outgoing root facts plus reachable blank support in the same graph.

    A named child has independent identity and needs its own selected entity or
    path policy. Blank list cells and shared blank descendants remain included.
    """
    pending = [key[0]]
    seen: set[PatchResource] = set()
    result: list[PatchQuad] = []
    while pending:
        budget.consume("work")
        node = pending.pop()
        if node in seen:
            continue
        budget.consume("states")
        seen.add(node)
        for quad in captured.witness.quads:
            budget.consume("work")
            if quad.subject != node or quad.graph != key[1]:
                continue
            budget.consume("states")
            budget.consume("bytes", 16)
            result.append(quad)
            if isinstance(quad.value, PatchBlank) and quad.value not in seen:
                budget.consume("states")
                pending.append(quad.value)
    return tuple(result)


def observe(
    entity: SelectedEntity,
    captured: Captured,
    path: RdfPath,
    collection: str,
    budget: Budget,
) -> tuple[tuple[PatchValue, ...], tuple[PatchQuad, ...]]:
    """Decode finite endpoints or an explicitly requested single ordered RDF list."""
    steps = finite_path_steps(path, budget)
    if steps is None:
        raise PatchUnavailable(
            "Instance policies support finite predicate/sequence/inverse paths"
        )
    values = path_values(entity.graph, entity.original.subject, steps, budget)
    if collection == "list":
        if len(values) != 1:
            raise PatchUnavailable(
                "An ordered list policy requires exactly one actual list head"
            )
        values = read_rdf_list(
            entity.graph, values[0], max_items=budget.limits.max_states, budget=budget
        )
    budget.consume("states", len(values))
    budget.consume("bytes", 16 * len(values))
    result = tuple(reference(value, captured, budget) for value in values)
    raw_support = record_support(captured, entity.key, budget)
    budget.consume("states", 2 * len(raw_support))
    budget.consume("bytes", 32 * len(raw_support))
    support, retained = list(raw_support), set(raw_support)
    # Native prefix traversal establishes intermediate membership. The exact
    # capture supplies witnesses, including inverse incoming edges.
    for index, (predicate, inverse) in enumerate(steps):
        prefix = path_values(
            entity.graph, entity.original.subject, steps[:index], budget
        )
        budget.consume("states", len(prefix))
        budget.consume("bytes", 16 * len(prefix))
        references = {reference(node, captured, budget) for node in prefix}
        for quad in captured.witness.quads:
            budget.consume("work")
            adjacent = quad.value if inverse else quad.subject
            if (
                quad.graph == entity.key[1]
                and quad.predicate == predicate
                and adjacent in references
                and quad not in retained
            ):
                budget.consume("states", 2)
                budget.consume("bytes", 32)
                retained.add(quad)
                support.append(quad)
    budget.consume("states", len(support))
    budget.consume("bytes", 8 * len(support))
    return result, tuple(support)
