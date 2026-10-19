"""One maintained transaction per bounded caller-managed ASK or SELECT."""

import json
from dataclasses import replace

from rdflib import URIRef

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope, _result
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.rdf.stores.protocol import failure
from bijux_ontology.rdf.stores.worker import _query
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report

from .types import ExternalStore


def query_external(
    source: ExternalStore,
    text: str,
    *,
    scope: QueryScope,
    max_rows: int = 1000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float | None = None,
) -> QueryResult:
    """Read one native transaction; later calls may observe newer committed data.

    Only inputs and retained outputs are metered. The caller-owned native engine
    executes in process, so a requested hard deadline is unavailable before work.
    Default/union/dataset scopes reuse the existing evaluator. A named projection
    cannot prove authored graph presence within this operation and is unsupported.
    Native source spelling is preserved, without reconstructing pre-store terms.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("External read limits conflict with the shared budget")
    if not isinstance(scope, QueryScope):
        raise InputError("External reads require an explicit QueryScope")
    operation = budget if budget is not None else Budget(limits)
    # QueryResult retains its mandatory scope, including rejected selectors.
    if scope.graph_name is not None:
        bound_text(scope.graph_name, operation)
    result = None
    try:
        if not isinstance(source, ExternalStore) or not isinstance(scope, QueryScope):
            raise InputError(
                "External reads require an adapter and explicit QueryScope"
            )
        if timeout is not None:
            raise ResourceError(
                "Caller-managed native reads cannot enforce a hard deadline"
            )
        if scope.kind == "named":
            raise ResourceError(
                "External named scope lacks same-transaction graph presence"
            )
        if not isinstance(text, str) or not text.strip():
            raise InputError("External queries require nonempty SPARQL text")
        bound_text(text, operation)
        if (
            type(max_rows) is not int
            or not 0 < max_rows <= operation.limits.max_triples
        ):
            raise InputError(
                "External max_rows must be positive and within max_triples"
            )
        response = _query(
            {
                "query": text,
                "scope": scope.kind,
                "graph": term_data(
                    URIRef(scope.graph_name) if scope.graph_name is not None else None
                ),
                "max_rows": max_rows,
            },
            source._store,
            operation,
        )
        operation.consume("bytes", json_bound(response, operation))
        raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
        result = _result(raw, scope, operation.limits, max_rows, budget=operation)
        return replace(
            result, report=replace(result.report, regime="native-single-read")
        )
    except LimitExceeded as error:
        issues = IssueCollector(operation)
        if result is not None:
            issues.retain_completed(result.report)
        issues.exhausted(str(error), location="rdf.external.query")
        report = Report(
            "rdf.external.query", issues=tuple(issues), requested=1, complete=False
        )
        return (
            QueryResult(report, scope)
            if result is None
            else replace(result, report=report)
        )
    except (InputError, OSError, SyntaxError) as error:
        typed = (
            ResourceError(str(error))
            if isinstance(error, LiteralScopeUnsupported)
            else error
            if isinstance(error, InputError)
            else ResourceError("Native external read failed")
        )
        return QueryResult(failure(typed, operation, "rdf.external.query"), scope)
