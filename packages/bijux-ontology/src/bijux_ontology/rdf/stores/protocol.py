"""Qualified immutable-store reads and shared bounded worker handoff."""

import json
import time
from dataclasses import asdict
from typing import Any

from rdflib import BNode, URIRef

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import BUDGET_KINDS, Budget, IssueCollector
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.reports import Issue, Report

from .seals import verify_files
from .types import StoredRdf


def scope_data(
    snapshot: StoredRdf, scope: QueryScope, operation: Budget
) -> dict[str, object]:
    """Qualify physical selectors, including ownership of blank named graphs."""
    if not isinstance(snapshot, StoredRdf) or not isinstance(scope, QueryScope):
        raise InputError("Disk reads require an owned snapshot and explicit QueryScope")
    if scope.kind == "named":
        found = False
        blank = isinstance(scope.graph_name, BNode)
        for name in snapshot.graph_names:
            operation.consume("work")
            if (name is scope.graph_name) if blank else (name == scope.graph_name):
                found = True
                break
        if not found:
            raise InputError(
                "Blank graph selector must belong to this disk snapshot"
                if blank
                else "Named graph is absent from the disk snapshot"
            )
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
    allowances = {kind: operation.remaining(kind) for kind in BUDGET_KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    raw = execute_worker(
        "bijux_ontology.rdf.stores.worker",
        payload,
        cap=operation.limits.max_total_bytes,
        timeout=max(0.000001, operation.deadline - time.monotonic()),
        cancel=operation.cancel,
    )
    if snapshot is not None:
        verify_files(snapshot.directory, snapshot.files, operation)
    response = decode_json(
        raw,
        max_bytes=operation.limits.max_total_bytes,
        max_depth=operation.limits.max_depth,
    )
    if not isinstance(response, dict) or not isinstance(response.get("status"), str):
        raise ResourceError("Malformed disk worker response")
    counts = response.pop("consumed", None)
    if not isinstance(counts, dict) or set(counts) != set(BUDGET_KINDS):
        if response["status"] == "exhausted":
            raise LimitExceeded(
                "Disk operation could not complete within its protocol bound"
            )
        raise ResourceError("Disk worker omitted shared consumption evidence")
    for kind in BUDGET_KINDS:
        amount = counts[kind]
        if type(amount) is not int or not 0 <= amount <= allowances[kind]:
            raise ResourceError("Disk worker returned invalid operation counters")
        operation.consume(kind, amount)
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
