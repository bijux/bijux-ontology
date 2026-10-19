"""Explicitly limited in-memory projections from immutable native snapshots."""

import threading

from rdflib import BNode, Dataset, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID

from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.queries.protocol import read_term
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import validate_triple
from bijux_ontology.reports import Report

from .protocol import execute, failure, scope_data
from .types import MaterializedRdf, StoredRdf


def materialize_snapshot(
    snapshot: StoredRdf,
    *,
    scope: QueryScope,
    max_triples: int,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> MaterializedRdf:
    """Count the selected native projection before allocating any RDFLib graph.

    Unsupported whole-graph consumers must opt into this materialization boundary.
    Named/default/union scopes produce only that graph; dataset scope retains
    authored physical names and nonempty-only inventory qualification.
    """
    operation = Operation(limits, budget, timeout, cancel)
    try:
        selected = scope_data(snapshot, scope, operation)
        if (
            type(max_triples) is not int
            or max_triples <= 0
            or max_triples > operation.limits.max_triples
        ):
            raise InputError(
                "Materialization requires a positive limit within max_triples"
            )
        response = execute(
            {"operation": "materialize", "maximum": max_triples, **selected},
            operation,
            snapshot,
        )
        if response.get("status") != "materialized" or not isinstance(
            response.get("rows"), list
        ):
            raise ResourceError(
                str(response.get("message", "Disk materialization incomplete"))
            )
        rows = response["rows"]
        if len(rows) > max_triples:
            raise ResourceError("Disk worker exceeded explicit materialization cap")
        dataset = Dataset(default_union=False)
        blanks: dict[str, BNode] = {}
        for row in rows:
            operation.consume("work")
            operation.consume("triples")
            if not isinstance(row, list) or len(row) != 4:
                raise ResourceError("Invalid materialized quad")
            subject, predicate, value, name = (read_term(item, blanks) for item in row)
            if (
                subject is None
                or predicate is None
                or value is None
                or name is not None
                and not isinstance(name, (URIRef, BNode))
            ):
                raise ResourceError("Invalid materialized RDF terms")
            if scope.kind == "dataset" and name == DATASET_DEFAULT_GRAPH_ID:
                raise ResourceError(
                    "RDFLib cannot separate this authored name from its default graph"
                )
            validate_triple(subject, predicate, value)
            target = (
                dataset.graph(name)
                if scope.kind == "dataset" and name is not None
                else dataset.default_graph
            )
            target.add((subject, predicate, value))
        source = PreparedSource._from_owned(
            dataset,
            is_dataset=scope.kind == "dataset",
            document_iri=None,
            effective_url=None,
            literal_fidelity="source_terms",
            max_triples=max_triples,
            graph_fidelity=snapshot.graph_fidelity
            if scope.kind == "dataset"
            else "complete",
        )
        return MaterializedRdf(
            source, Report("rdf.store.materialization", 1, requested=1)
        )
    except InputError as error:
        return MaterializedRdf(
            None, failure(error, operation, "rdf.store.materialization")
        )
