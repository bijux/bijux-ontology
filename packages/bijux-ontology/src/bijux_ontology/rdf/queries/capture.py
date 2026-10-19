"""Bounded selected-input capture and finite local query request ownership."""

import json
import threading
from collections.abc import Iterator
from dataclasses import asdict
from typing import cast

from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, BudgetKind, Limits
from bijux_ontology.execution.processes.transport import checkpoint
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.protocol import QueryTerm, term_data
from bijux_ontology.rdf.sources.selection import select_read
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import validate_triple

from .types import QueryScope


class CaptureBudget(Budget):
    """Check the host deadline during metadata scans owned by RDF snapshots."""

    def __init__(self, limits: Limits, deadline: float, cancel: threading.Event | None):
        super().__init__(limits)
        self.deadline, self.cancel = deadline, cancel

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Check cancellation and deadline before charging captured work."""
        checkpoint(self.deadline, self.cancel)
        super().consume(kind, amount)


def selected_graphs(
    source: Graph | PreparedSource,
    scope: QueryScope,
    budget: Budget,
) -> Iterator[tuple[QueryTerm | None, Graph]]:
    """Inspect only the requested resource scope through the owned read boundary."""
    if isinstance(source, PreparedSource):
        selected = select_read(
            source,
            budget,
            graph_name="@default" if scope.kind == "default" else scope.graph_name,
            union=scope.kind == "union",
            dataset=scope.kind == "dataset",
        )
        if scope.kind == "dataset":
            yield from selected.contexts(budget)
        else:
            yield None, selected.read()
        return
    if not isinstance(source, Graph):
        raise InputError("Queries require a graph or prepared source")
    is_dataset = isinstance(source, (Dataset, ConjunctiveGraph))
    if scope.kind == "named":
        if isinstance(scope.graph_name, BNode):
            raise InputError("Blank graph names require an owned PreparedSource")
        if not is_dataset:
            raise InputError("Named query scope requires a dataset")
        assert isinstance(source, (Dataset, ConjunctiveGraph))
        contexts = source.graphs() if isinstance(source, Dataset) else source.contexts()
        for graph in contexts:
            budget.consume("work")
            budget.consume("states")
            if graph.identifier == scope.graph_name:
                yield None, graph
                return
        raise InputError("Selected graph is absent from the supplied dataset")
    if not is_dataset:
        yield None, source
        return
    assert isinstance(source, (Dataset, ConjunctiveGraph))
    default = (
        source.default_graph if isinstance(source, Dataset) else source.default_context
    )
    if scope.kind == "default":
        yield None, default
        return
    contexts = source.graphs() if isinstance(source, Dataset) else source.contexts()
    for graph in contexts:
        budget.consume("work")
        budget.consume("states")
        yield (
            None
            if scope.kind == "union" or graph.identifier == default.identifier
            else cast(URIRef | BNode, graph.identifier),
            graph,
        )


def capture_request(
    source: Graph | PreparedSource,
    text: str,
    scope: QueryScope,
    max_rows: int,
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> bytes:
    """Capture bounded typed input, including empty named graph inventory."""
    budget.consume("bytes", len(text.encode()))
    records = []
    namespaces: dict[str, str] = {}
    for name, graph in selected_graphs(source, scope, budget):
        checkpoint(deadline, cancel)
        budget.consume("resources")
        rows = []
        size = 0
        for triple in graph:
            checkpoint(deadline, cancel)
            budget.consume("work")
            budget.consume("triples")
            validate_triple(*triple)
            row = [term_data(term) for term in triple]
            encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()
            size += len(encoded)
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Query input graph exceeds max_bytes")
            budget.consume("bytes", len(encoded))
            rows.append(row)
        if name is not None and not isinstance(name, (URIRef, BNode)):
            raise InputError("Query graph names must be IRIs or owned blanks")
        encoded_name = term_data(name)
        budget.consume("bytes", len(json.dumps(encoded_name).encode()))
        records.append((encoded_name, rows))
        for prefix, iri in graph.namespaces():
            budget.consume("work")
            namespace = absolute_iri(str(iri))
            if prefix in namespaces and namespaces[prefix] != namespace:
                raise InputError(
                    "Query namespace prefix has conflicting supplied bindings"
                )
            if prefix not in namespaces:
                budget.consume("bytes", len(prefix.encode()) + len(namespace.encode()))
                namespaces[prefix] = namespace
    request = {
        "query": text,
        "graphs": records,
        "namespaces": list(namespaces.items()),
        "dataset": scope.kind == "dataset",
        "max_rows": max_rows,
        "limits": asdict(budget.limits),
        "output_cap": budget.limits.max_total_bytes,
    }
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    remaining = budget.limits.max_total_bytes - len(payload)
    if remaining < 256:
        raise LimitExceeded("Local query input leaves no bounded response allowance")
    request["output_cap"] = remaining
    return json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
