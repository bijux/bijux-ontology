"""Bounded local ASK/SELECT against sealed native disk snapshots."""

import json
import threading
from dataclasses import replace

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope, _result
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from .protocol import execute, failure, scope_data
from .types import StoredRdf


def query_snapshot(
    snapshot: StoredRdf,
    text: str,
    *,
    scope: QueryScope,
    max_rows: int = 1000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: threading.Event | None = None,
) -> QueryResult:
    """Execute supported local queries without exporting the complete RDF dataset.

    The existing maintained parser excludes retrieval and updates. Native datatype
    construction is outside this exact lexical profile; noncanonical constants
    cannot silently become different RDF terms. Physical graph projections cannot
    expose other named graphs. Blank answers remain local to each result.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    result = None
    try:
        selected = scope_data(snapshot, scope, operation)
        if (
            not isinstance(text, str)
            or not text.strip()
            or type(max_rows) is not int
            or max_rows <= 0
        ):
            raise InputError("Disk queries require nonempty text and positive max_rows")
        bound_text(text, operation)
        response = execute(
            {"operation": "query", "query": text, "max_rows": max_rows, **selected},
            operation,
            snapshot,
        )
        raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
        operation.consume("bytes", len(raw))
        result = _result(raw, scope, operation.limits, max_rows, budget=operation)
        retain_report(result.report, operation, issues)
        if scope.kind == "dataset" and snapshot.graph_fidelity != "complete":
            issues.append(
                Issue(
                    "rdf.graph_scope_unverified",
                    "Disk source lacks complete empty named graph inventory",
                    kind="unverified",
                )
            )
            return replace(
                result,
                report=Report(
                    "rdf.query",
                    result.report.checked,
                    tuple(issues),
                    requested=1,
                    complete=False,
                    children=(result.report,),
                ),
            )
        return result
    except LimitExceeded as error:
        if result is not None:
            issues.retain_completed(result.report)
        issues.exhausted(str(error), location="rdf.query")
        report = Report("rdf.query", issues=tuple(issues), requested=1, complete=False)
        return (
            QueryResult(report, scope)
            if result is None
            else replace(result, report=report)
        )
    except InputError as error:
        return QueryResult(failure(error, operation, "rdf.query"), scope)
