"""Exact physical source capture, record anchors and owned joint RDF input."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.snapshots import PreparedSource, copy_graph
from bijux_ontology.rdf.terms import bound_node

from .types import EmittedScope


def capture_bindings(
    raw: Sequence[RdfModelUsage], budget: Budget
) -> tuple[RdfModelUsage, ...]:
    """Prepay the full original anchor inventory before any import callback."""
    if not isinstance(raw, Sequence) or isinstance(cast(object, raw), str | bytes):
        raise InputError("Emitted RDF bindings require a finite typed sequence")
    size = len(raw)
    if not size:
        raise InputError("No required emitted record bindings were supplied")
    budget.consume("states", size * 4)
    budget.consume("bytes", 128 + size * 512)
    result = []
    for index in range(size):
        budget.consume("work")
        item = raw[index]
        if type(item) is not RdfModelUsage:
            raise InputError("Emitted records require ordinary RdfModelUsage bindings")
        bound_text(item.model, budget)
        bound_text(item.location, budget)
        bound_node(item.subject, budget)
        result.append(item)
    return tuple(result)


def selected_scope(
    source: PreparedSource, budget: Budget
) -> tuple[EmittedScope, Graph]:
    """Keep the actual original graph pin instead of inferring a union."""
    if not isinstance(source, PreparedSource):
        raise ResourceError(
            "Emitted diagnosis currently requires prepared RDF ontology and data"
        )
    if source.union or source.literal_fidelity not in (
        "source_terms",
        "supplied_terms",
    ):
        raise ResourceError("Emitted diagnosis requires a qualified physical RDF graph")
    found = None
    for kind, name, graph in source._contexts(budget=budget):
        if found is not None:
            raise ResourceError(
                "Emitted diagnosis requires one selected physical graph"
            )
        if name is not None:
            bound_node(name, budget)
        budget.consume("states", 2)
        budget.consume("bytes", 128)
        found = EmittedScope(source, kind, name, len(graph)), graph
    if found is None:
        raise ResourceError("The selected RDF graph has no established physical scope")
    return found


def participation(
    graph: Graph, budget: Budget
) -> tuple[dict[URIRef | int, int], dict[int, BNode]]:
    """Count actual source statements containing each node, once per statement."""
    counts: dict[URIRef | int, int] = {}
    blanks: dict[int, BNode] = {}
    budget.consume("states", 4 * len(graph))
    budget.consume("bytes", 128 + 256 * len(graph))
    for subject, predicate, value in graph:
        budget.consume("work")
        for node in (subject, predicate, value):
            bound_node(node, budget)
        if predicate == OWL.imports:
            raise ResourceError(
                "Emitted data cannot expand the selected ontology import closure"
            )
        keys: list[URIRef | int] = []
        for node in (subject, value):
            if isinstance(node, BNode):
                blanks[id(node)] = node
                keys.append(id(node))
            elif isinstance(node, URIRef):
                keys.append(node)
        for index, key in enumerate(keys):
            if index == 0 or key != keys[0]:
                counts[key] = counts.get(key, 0) + 1
    return counts, blanks


def joint_source(
    ontology: Graph, emitted: Graph, original: PreparedSource, budget: Budget
) -> PreparedSource:
    """Combine the two qualified roles with maintained document blank separation."""
    total = len(ontology) + len(emitted)
    budget.consume("states", 8 + total * 8)
    budget.consume("bytes", 1024 + total * 768)
    budget.consume("triples", total)
    budget.consume("resources")
    for graph in (ontology, emitted):
        for triple in graph:
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
    # The closure factory checked original cross-document blank ownership first.
    # Equal labels from independently owned documents still stay separate here.
    schema = copy_graph(ontology, max_triples=budget.limits.max_triples)
    assertions = copy_graph(emitted, max_triples=budget.limits.max_triples)
    dataset = Dataset()
    for graph in (schema, assertions):
        for triple in graph:
            budget.consume("work")
            dataset.default_graph.add(triple)
    return PreparedSource._from_owned(
        dataset,
        is_dataset=False,
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity="supplied_terms",
        max_triples=budget.limits.max_triples,
    )
