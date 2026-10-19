"""Ordinary declaration checks over one selected caller-managed native read."""

from collections.abc import Sequence

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.stores.external.checking import read_external_declarations
from bijux_ontology.rdf.stores.external.types import ExternalStore

from .checking import check_vocabulary


def check_external_terms(
    source: ExternalStore,
    terms: Sequence[TermDeclaration],
    *,
    scope: QueryScope,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check explicit selected kinds without copying the complete caller store.

    Native transaction evidence is required alongside the existing checker. Each
    call is a new read, not a persistent immutable revision shared across calls.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("External declaration limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children = []
    result = None
    checked, requested = 0, 1
    complete = True
    try:
        captured = capture_items(terms, TermDeclaration, operation)
        requested = max(1, len(captured))
        operation.consume("states", len(captured))
        for term in captured:
            bound_text(term.iri, operation)
            bound_text(term.location, operation)
            for kind in capture_items(term.kinds, str, operation):
                bound_text(kind, operation)
        projection = read_external_declarations(
            source,
            tuple(term.iri for term in captured),
            scope=scope,
            budget=operation,
        )
        retain_report(projection.report, operation, issues)
        children.append(projection.report)
        if projection.source is not None and projection.report.verified:
            result = check_vocabulary(
                projection.source._read(budget=operation),
                captured,
                budget=operation,
            )
            checked = result.checked
            retain_report(result, operation, issues)
            children.append(result)
    except LimitExceeded as error:
        complete = False
        if result is not None:
            issues.retain_completed(result)
        issues.exhausted(str(error), location="vocabulary.external_declarations")
    return Report(
        "vocabulary.external_declarations",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        regime="native-single-read",
        children=tuple(children),
    )
