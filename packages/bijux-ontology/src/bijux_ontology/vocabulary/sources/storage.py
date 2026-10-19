"""Exact selected-term inspection and declaration checks over sealed disk RDF."""

from collections.abc import Sequence

from rdflib import Graph, URIRef

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.stores.queries import query_snapshot
from bijux_ontology.rdf.stores.types import StoredRdf
from bijux_ontology.rdf.terms import bound_text, validate_triple
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Selection, TermInfo
from bijux_ontology.vocabulary.checking import check_vocabulary


def _project(
    snapshot: StoredRdf, iris: Sequence[str], scope: QueryScope, budget: Budget
) -> tuple[Graph | None, Report]:
    """Fetch every selected identifier occurrence in one bounded native query."""
    if not isinstance(iris, Sequence) or isinstance(iris, (str, bytes)):
        raise InputError("Stored term inspection needs a finite IRI sequence")
    budget.consume("states", len(iris))
    selected = []
    for iri in iris:
        budget.consume("work")
        bound_text(iri, budget)
        selected.append(URIRef(absolute_iri(iri)).n3())
    if not selected:
        raise InputError("Stored term inspection requires at least one IRI")
    values = ",".join(selected)
    query = (
        "SELECT DISTINCT ?s ?p ?o WHERE { ?s ?p ?o . FILTER "
        f"(?s IN ({values}) || ?p IN ({values}) || ?o IN ({values}) "
        f"|| (isLiteral(?o) && datatype(?o) IN ({values}))) "
        "}"
    )
    result = query_snapshot(
        snapshot, query, scope=scope, max_rows=budget.limits.max_triples, budget=budget
    )
    if not result.report.verified:
        return None, result.report
    graph = Graph(bind_namespaces="none")
    for row in result.rows:
        budget.consume("work")
        budget.consume("triples")
        if len(row) != 3 or any(value is None for value in row):
            raise InputError("Selected native RDF row has no complete triple")
        subject, predicate, value = row
        assert subject is not None and predicate is not None and value is not None
        validate_triple(subject, predicate, value)
        graph.add((subject, predicate, value))
    return graph, result.report


def inspect_stored_terms(
    snapshot: StoredRdf,
    iris: Sequence[str],
    *,
    scope: QueryScope,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Selection[TermInfo]:
    """Reuse ordinary asserted lookup over complete selected incident statements.

    Only the selected term neighbourhood is retained. This does not construct a
    whole-vocabulary hierarchy index or claim complete reference/lint coverage.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Stored inspection limits conflict with shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    items = []
    children = []
    requested = 1
    try:
        iris = capture_items(iris, str, operation)
        requested = max(1, len(iris))
        graph, report = _project(snapshot, iris, scope, operation)
        retain_report(report, operation, issues)
        children.append(report)
        if graph is not None:
            view = VocabularyView(graph, budget=operation)
            for iri in iris:
                operation.consume("states")
                items.append(view.lookup(iri))
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Selection(
        tuple(items),
        Report(
            "vocabulary.stored_inspection",
            len(items),
            tuple(issues),
            requested=requested,
            children=tuple(children),
        ),
    )


def check_stored_terms(
    snapshot: StoredRdf,
    terms: Sequence[TermDeclaration],
    *,
    scope: QueryScope,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check explicit declaration kinds without whole-dataset materialization."""
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Stored declaration limits conflict with shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children = []
    checked = 0
    requested = 1
    try:
        terms = capture_items(terms, TermDeclaration, operation)
        requested = max(1, len(terms))
        operation.consume("states", len(terms))
        if any(not isinstance(term, TermDeclaration) for term in terms):
            raise InputError("Stored declarations require TermDeclaration values")
        graph, projection = _project(
            snapshot, tuple(term.iri for term in terms), scope, operation
        )
        retain_report(projection, operation, issues)
        children.append(projection)
        if graph is not None:
            result = check_vocabulary(graph, terms, budget=operation)
            retain_report(result, operation, issues)
            children.append(result)
            checked = result.checked
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Report(
        "vocabulary.stored_declarations",
        checked,
        tuple(issues),
        requested=requested,
        children=tuple(children),
    )
