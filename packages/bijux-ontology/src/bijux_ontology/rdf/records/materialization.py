"""Exact owned RDF materialization independent of patch transactions."""

from typing import cast

from rdflib import BNode, Dataset, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import literal_term

from .types import PatchBlank, PatchLiteral, PatchValue, SnapshotWitness


def materialize(
    witness: SnapshotWitness,
    original: PreparedSource,
    operation: Budget,
    *,
    bindings: dict[str, BNode] | None = None,
) -> PreparedSource:
    """Own exact records and optionally expose their fresh anonymous identities.

    The optional empty caller dictionary receives bindings only after complete
    materialization. Each token denotes the actual node in the returned owner,
    never a parser label or a node from the original input snapshot.
    """
    if bindings is not None and (type(bindings) is not dict or bindings):
        raise InputError("Materialization bindings require an empty plain dictionary")
    operation.consume("states", len(witness.quads) + len(witness.graphs))
    dataset = Dataset()
    blanks: dict[str, BNode] = {}

    def node(term: PatchValue) -> Node:
        """Allocate one fresh owned blank for every shared portable reference."""
        operation.consume("work")
        if isinstance(term, PatchBlank):
            if term.token not in blanks:
                operation.consume("states")
                operation.consume("bytes", 64)
                blanks[term.token] = BNode()
            return blanks[term.token]
        if isinstance(term, PatchLiteral):
            for text in (term.lexical, term.datatype, term.language or ""):
                bound_text(text, operation)
            return literal_term(
                term.lexical, datatype=term.datatype, language=term.language
            )
        bound_text(term, operation)
        return URIRef(term)

    for graph in witness.graphs:
        operation.consume("resources")
        dataset.graph(cast(URIRef | BNode, node(graph)))
    for quad in witness.quads:
        operation.consume("work")
        operation.consume("triples")
        output_graph = (
            dataset.default_graph
            if quad.graph is None
            else dataset.graph(cast(URIRef | BNode, node(quad.graph)))
        )
        output_graph.add((node(quad.subject), node(quad.predicate), node(quad.value)))
    selection: GraphSelection | None = None
    if witness.kind == "named":
        assert witness.graph_selection is not None
        selection = cast(URIRef | BNode, node(witness.graph_selection))
    elif witness.kind == "default":
        selection = "@default"
    operation.consume("work", len(witness.quads) + len(witness.graphs))
    operation.consume("triples", len(witness.quads))
    for metadata in (original.document_iri, original.effective_url):
        if metadata is not None:
            bound_text(metadata, operation)
    result = PreparedSource._from_owned(
        dataset,
        is_dataset=witness.kind != "graph",
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity=original.literal_fidelity,
        graph_fidelity="complete",
        graph_name=selection if not isinstance(selection, BNode) else None,
        max_triples=operation.limits.max_triples,
    )
    if isinstance(selection, BNode):
        # Selection now refers to the exact fresh graph allocated by node().
        result.graph_name = selection
    if bindings is not None:
        operation.consume("states", len(blanks))
        operation.consume("bytes", 64 * len(blanks))
        operation.consume("work", len(blanks))
        bindings.update(blanks)
    return result
