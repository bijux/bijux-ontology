"""Typed read-only local query results with explicit owned RDF scope."""

import threading
import time
from math import isfinite

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, IssueKind, Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.execution.processes.transport import checkpoint
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.rdf.queries import types as query_types
from bijux_ontology.rdf.queries.capture import CaptureBudget, capture_request
from bijux_ontology.rdf.queries.results import adopt_result
from bijux_ontology.rdf.sources.snapshots import PreparedSource


def query_rdf(
    source: Graph | PreparedSource,
    text: str,
    *,
    scope: query_types.QueryScope,
    max_rows: int = 1000,
    limits: Limits | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> query_types.QueryResult:
    """Run local ASK/SELECT with explicit graph scope and an owned deadline.

    Parser and query evaluation run in a disposable process. SERVICE and every
    FROM clause are prohibited using the maintained parse tree. Limits bound
    capture, worker parsing/evaluation, and reply validation independently;
    counters are local to those stages, with one operation-wide deadline.
    Worker work counts input records, syntax and retained solutions; opaque
    engine search uses the real deadline, not an instruction counter. Namespace
    bindings come only from supplied selected graphs or explicit query PREFIX.
    """
    return _query_rdf(
        source,
        text,
        scope=scope,
        max_rows=max_rows,
        limits=limits,
        timeout=timeout,
        cancel=cancel,
        owned_worker=False,
        deadline=None,
    )


def _query_rdf(
    source: Graph | PreparedSource,
    text: str,
    *,
    scope: query_types.QueryScope,
    max_rows: int = 1000,
    limits: Limits | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
    owned_worker: bool,
    deadline: float | None,
) -> query_types.QueryResult:
    """Keep independent stage capacities inside the enclosing absolute deadline.

    Only fixed worker dispatchers select inline native execution; their caller
    owns opaque-engine cancellation and process cleanup.
    """
    if not isinstance(scope, query_types.QueryScope):
        raise InputError("Queries require an explicit QueryScope")
    if not isinstance(text, str) or not text.strip():
        raise InputError("Queries require nonempty SPARQL text")
    if type(max_rows) is not int or max_rows <= 0:
        raise InputError("max_rows must be a positive integer")
    if type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
        raise InputError("Query timeout must be finite and positive")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Query cancellation requires a threading.Event")
    selected = limits if limits is not None else Limits()
    local_deadline = time.monotonic() + timeout
    deadline = local_deadline if deadline is None else min(deadline, local_deadline)
    budget = CaptureBudget(selected, deadline, cancel)
    try:
        checkpoint(deadline, cancel)
        if len(text.encode()) > selected.max_bytes:
            raise LimitExceeded("SPARQL text exceeds max_bytes")
        payload = capture_request(
            source, text, scope, max_rows, budget, deadline, cancel
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("RDF operation exceeded its wall-clock deadline")
        if owned_worker:
            # The fixed project worker's host owns cancellation and its absolute
            # job deadline, including opaque native search. A second process
            # group would escape that ownership when the project is stopped.
            from bijux_ontology.rdf.queries.execution import process_request

            raw = process_request(payload, selected.max_total_bytes)
        else:
            raw = execute_worker(
                "bijux_ontology.rdf.queries.worker",
                payload,
                cap=selected.max_total_bytes,
                timeout=remaining,
                cancel=cancel,
            )
        budget.consume("bytes", len(raw))
        result = adopt_result(raw, scope, selected, max_rows)
        checkpoint(deadline, cancel)
        if (
            isinstance(source, PreparedSource)
            and scope.kind == "dataset"
            and source.graph_fidelity != "complete"
        ):
            from dataclasses import replace

            return replace(
                result,
                report=Report(
                    "rdf.query",
                    1,
                    result.report.issues
                    + (
                        Issue(
                            "rdf.graph_scope_unverified",
                            "Input does not establish complete empty-graph inventory",
                            kind="unverified",
                        ),
                    ),
                    requested=1,
                    complete=False,
                    families=result.report.families,
                ),
            )
        return result
    except (InputError, ResourceError) as error:
        kind: IssueKind = (
            "exhausted" if isinstance(error, LimitExceeded) else "input_error"
        )
        return query_types.QueryResult(
            Report(
                "rdf.query",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "execution.limit_exceeded"
                        if kind == "exhausted"
                        else "input.invalid",
                        str(error),
                        kind=kind,
                    ),
                ),
            ),
            scope,
        )
