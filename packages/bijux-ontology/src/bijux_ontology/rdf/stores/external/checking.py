"""Bounded selected declaration projection from one native read transaction."""

from collections.abc import Sequence

from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.stores.protocol import failure
from bijux_ontology.rdf.stores.types import MaterializedRdf
from bijux_ontology.rdf.terms import bound_node, validate_triple
from bijux_ontology.reports import Report

from .queries import query_external
from .types import ExternalStore


def read_external_declarations(
    source: ExternalStore,
    iris: Sequence[str],
    *,
    scope: QueryScope,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> MaterializedRdf:
    """Retain only selected rdf:type triples, never a whole-store RDFLib copy.

    All selected subjects are read by the same native query transaction. This
    finite projection supports explicit declaration checks, not ontology closure,
    reference integrity, whole-vocabulary coverage or imports completeness.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("External projection limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    projection = None
    try:
        if not isinstance(scope, QueryScope) or scope.kind not in {"default", "union"}:
            raise ResourceError("Declaration projection needs default or union scope")
        captured = capture_items(iris, str, operation)
        if not captured:
            raise InputError("External declarations require a nonempty IRI inventory")
        selected = []
        prefix = "SELECT ?s ?o WHERE { VALUES ?s { "
        suffix = " } ?s " + RDF.type.n3() + " ?o }"
        size = len(prefix) + len(suffix)
        for iri in captured:
            bound_text(iri, operation)
            if isinstance(iri, (BNode, Literal)):
                raise InputError("External declaration selectors must be named IRIs")
            rendered = URIRef(absolute_iri(iri)).n3()
            retained = len(rendered.encode()) + 1
            size += retained
            if size > operation.limits.max_bytes:
                raise LimitExceeded("External declaration query exceeds max_bytes")
            operation.consume("bytes", retained)
            selected.append(rendered)
        operation.consume("bytes", size)
        query = prefix + " ".join(selected) + suffix
        projection = query_external(
            source,
            query,
            scope=scope,
            max_rows=operation.limits.max_triples,
            budget=operation,
        )
        retain_report(projection.report, operation, issues)
        if not projection.report.verified:
            return MaterializedRdf(None, projection.report)
        operation.consume("states")
        dataset = Dataset(default_union=False)
        graph = dataset.default_graph
        for subject, value in projection.rows:
            operation.consume("work")
            operation.consume("triples")
            operation.consume("states")
            if subject is None or value is None:
                raise ResourceError("Native declaration projection omitted an RDF term")
            bound_node(subject, operation)
            bound_node(value, operation)
            validate_triple(subject, RDF.type, value)
            graph.add((subject, RDF.type, value))
        return MaterializedRdf(
            PreparedSource._from_owned(
                dataset,
                is_dataset=False,
                document_iri=None,
                effective_url=None,
                literal_fidelity="source_terms",
                graph_fidelity="complete",
            ),
            projection.report,
        )
    except LimitExceeded as error:
        if projection is not None:
            issues.retain_completed(projection.report)
        issues.exhausted(str(error), location="rdf.external.declarations")
        return MaterializedRdf(
            None,
            Report(
                "rdf.external.declarations",
                issues=tuple(issues),
                requested=1,
                complete=False,
            ),
        )
    except InputError as error:
        return MaterializedRdf(
            None, failure(error, operation, "rdf.external.declarations")
        )
