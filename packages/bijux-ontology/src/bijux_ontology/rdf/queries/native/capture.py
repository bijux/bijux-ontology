"""Bounded immutable selected input for maintained finite native query adapters."""

from typing import cast

from rdflib import Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import bound_text
from bijux_ontology.rdf.queries.capture import selected_graphs
from bijux_ontology.rdf.queries.protocol import QueryTerm, term_data
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, validate_triple

from .types import GraphRecord, NativeQueryInput, QueryBinding


def capture_selected_input(
    source: PreparedSource,
    scope: QueryScope,
    bindings: tuple[QueryBinding, ...],
    operation: Operation,
) -> NativeQueryInput:
    """Capture exact physical input once, independent of later declarations."""
    if type(source) is not PreparedSource or not isinstance(scope, QueryScope):
        raise InputError("Native input requires an owned PreparedSource and QueryScope")
    if type(bindings) is not tuple or any(
        type(item) is not QueryBinding for item in bindings
    ):
        raise InputError("Native bindings require a finite tuple of QueryBinding")
    operation.consume("states", len(bindings) + 6)
    operation.consume("bytes", 128 * (len(bindings) + 1))
    metadata = SnapshotMetadata.capture(source, operation)
    if scope.kind == "dataset" and metadata.graph_fidelity != "complete":
        raise ResourceError("Dataset workload requires complete named graph inventory")
    records: list[GraphRecord] = []
    namespaces: dict[str, str] = {}
    for name, graph in selected_graphs(source, scope, operation):
        operation.consume("resources")
        operation.consume("states")
        operation.consume("bytes", 128)
        if name is not None:
            bound_node(name, operation)
        rows = []
        for triple in graph:
            operation.consume("work")
            operation.consume("triples")
            operation.consume("states", 4)
            operation.consume("bytes", 256)
            validate_triple(*triple)
            for term in triple:
                bound_node(term, operation)
            rows.append(cast(tuple[QueryTerm, QueryTerm, QueryTerm], triple))
        records.append((name, tuple(rows)))
        for prefix, iri in graph.namespaces():
            operation.consume("work")
            bound_text(prefix, operation)
            bound_node(URIRef(iri), operation)
            text = str(iri)
            if prefix in namespaces and namespaces[prefix] != text:
                raise InputError("Selected graphs have conflicting namespace bindings")
            if prefix not in namespaces:
                operation.consume("states")
                operation.consume("bytes", 64)
                namespaces[prefix] = text
    operation.consume("states", len(namespaces))
    operation.consume("bytes", len(namespaces) * 64)
    if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
        raise ResourceError("RDF source metadata changed during workload capture")
    names = set()
    for binding in bindings:
        bound_text(binding.variable, operation)
        bound_node(binding.value, operation)
        if binding.variable in names:
            raise InputError("Workload binding variables must be distinct")
        names.add(binding.variable)
    return NativeQueryInput._new(
        scope, metadata, tuple(records), tuple(namespaces.items()), bindings
    )


def wire_input(selected: NativeQueryInput, operation: Operation) -> dict[str, object]:
    """Pay each run's mutable protocol representation before its allocation."""
    operation.consume("states", 3)
    operation.consume("bytes", 512)
    graphs = []
    for name, triples in selected.graphs:
        operation.consume("work")
        operation.consume("states", 3)
        operation.consume("bytes", 384)
        if name is not None:
            bound_node(name, operation)
        rows = []
        for triple in triples:
            operation.consume("work")
            operation.consume("states", 10)
            operation.consume("bytes", 1024)
            for term in triple:
                bound_node(term, operation)
            for term in triple:
                operation.consume("bytes", 4 * len(term))
                if isinstance(term, Literal):
                    operation.consume(
                        "bytes",
                        4 * (len(term.datatype or "") + len(term.language or "")),
                    )
            rows.append([term_data(term) for term in triple])
        if name is not None:
            operation.consume("bytes", 4 * len(name))
        graphs.append([term_data(name), rows])
    operation.consume("states", 3 * len(selected.bindings))
    operation.consume("bytes", 384 * len(selected.bindings))
    bindings = []
    for binding in selected.bindings:
        bound_text(binding.variable, operation)
        bound_node(binding.value, operation)
        operation.consume("bytes", 4 * len(binding.value))
        if isinstance(binding.value, Literal):
            operation.consume(
                "bytes",
                4
                * (
                    len(binding.value.datatype or "")
                    + len(binding.value.language or "")
                ),
            )
        bindings.append([binding.variable, term_data(binding.value)])
    return {"graphs": graphs, "namespaces": selected.namespaces, "bindings": bindings}
