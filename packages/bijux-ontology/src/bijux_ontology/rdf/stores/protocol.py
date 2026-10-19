"""Qualified immutable-store reads and shared bounded worker handoff."""

import json
import time
from dataclasses import asdict
from typing import Any

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, IssueCollector
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.receipts import TerminalResponse
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.rdf.sources.snapshots import owned_graph_name
from bijux_ontology.resources.files import verify_files

from .types import StoredRdf


def scope_data(
    snapshot: StoredRdf, scope: QueryScope, operation: Budget
) -> dict[str, object]:
    """Qualify physical selectors, including ownership of blank named graphs."""
    if not isinstance(snapshot, StoredRdf) or not isinstance(scope, QueryScope):
        raise InputError("Disk reads require an owned snapshot and explicit QueryScope")
    if scope.kind == "named":
        assert scope.graph_name is not None
        owned_graph_name(snapshot.graph_names, scope.graph_name, operation)
    operation.consume("work")
    return {
        "scope": scope.kind,
        "graph": term_data(
            scope.graph_name
            if isinstance(scope.graph_name, BNode)
            else URIRef(scope.graph_name)
            if scope.graph_name is not None
            else None
        ),
    }


def execute(
    request: dict[str, Any], operation: Operation, snapshot: StoredRdf | None = None
) -> dict[str, Any]:
    """Adopt counters and completion only after the immutable revision still matches."""
    if snapshot is not None:
        verify_files(snapshot.directory, snapshot.files, operation)
        request["database"] = str(snapshot.directory)
    request["limits"] = asdict(operation.limits)
    request["allowances"] = {kind: operation.remaining(kind) for kind in BUDGET_KINDS}
    operation.consume("bytes", json_bound(request, operation))
    terminal = TerminalResponse(operation, operation.limits.max_total_bytes)
    allowances = {kind: terminal.available(kind) for kind in BUDGET_KINDS}
    # The native evaluator returns data/counters; host adoption owns Issues.
    allowances["issues"] = 0
    delegated = DelegatedAllowance(operation, allowances)
    request["allowances"] = delegated.to_data()
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    dispatched = received = False
    try:
        terminal.admit(payload)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.rdf.stores.worker",
            payload,
            cap=terminal.cap,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=operation.cancel,
        )
        response = terminal.decode(raw)
        if not isinstance(response, dict) or not isinstance(
            response.get("status"), str
        ):
            raise ResourceError("Malformed disk worker response")
        counts = response.pop("consumed", None)
        if counts is None and response["status"] == "exhausted":
            raise LimitExceeded(
                "Disk operation could not complete within its protocol bound"
            )
        delegated.settle(counts)
        received = True
    finally:
        try:
            terminal.close()
        finally:
            if dispatched and not received:
                delegated.forfeit()
    operation.consume("work", 0)
    if snapshot is not None:
        verify_files(snapshot.directory, snapshot.files, operation)
    if response["status"] == "exhausted":
        raise LimitExceeded(str(response.get("message", "Disk operation exhausted")))
    return response


def failure(error: InputError, operation: Budget, scope: str) -> Report:
    """Keep unsupported, invalid and exhausted work distinct from native findings."""
    issues = IssueCollector(operation)
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error), location=scope)
    else:
        try:
            issues.append(
                Issue(
                    "rdf.store_unverified"
                    if isinstance(error, ResourceError)
                    else "input.invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location=scope)
    return Report(scope, issues=tuple(issues), requested=1, complete=False)
