"""Bounded profile inventory capture and selected immutable RDF containers."""

from collections.abc import Sequence

from rdflib import BNode, Dataset, Graph, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .types import Triple


def _capture(values: Sequence[object], budget: Budget) -> tuple[object, ...]:
    """Own one bounded finite inventory using one cached count and indexed reads."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Shape profiles require finite typed sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            result.append(values[index])
        except IndexError as error:
            raise InputError(
                "Shape profile inventory changed during capture"
            ) from error
    return tuple(result)


def _owned(
    original: PreparedSource,
    graph: Graph,
    triples: Sequence[Triple],
    budget: Budget,
    *,
    selection: GraphSelection | None = None,
) -> PreparedSource:
    """Adopt one new selected container, sharing only immutable owned RDF terms."""
    name = original.graph_name if selection is None else selection
    if isinstance(name, BNode):
        raise InputError("Portable profile resources require named/default graph scope")
    if name is not None:
        bound_text(name, budget)
        name = str(name)
    dataset = Dataset()
    target = (
        dataset.graph(URIRef(name))
        if name not in (None, "@default")
        else dataset.default_graph
    )
    for prefix, namespace in graph.namespaces():
        budget.consume("states")
        bound_text(prefix, budget)
        bound_node(namespace, budget)
        dataset.bind(prefix, namespace)
    for triple in triples:
        budget.consume("work")
        budget.consume("triples")
        for term in triple:
            bound_node(term, budget)
        target.add(triple)
    for metadata in (original.document_iri, original.effective_url):
        if metadata is not None:
            bound_text(metadata, budget)
    return PreparedSource._from_owned(
        dataset,
        is_dataset=original.is_dataset,
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity=original.literal_fidelity,
        graph_fidelity="complete",
        graph_name=name,
        max_triples=budget.limits.max_triples,
    )
