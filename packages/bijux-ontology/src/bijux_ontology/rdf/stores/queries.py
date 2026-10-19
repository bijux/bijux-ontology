"""Shared bounded ASK/SELECT for sealed snapshots and caller-owned stores."""

import json
import threading
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pyoxigraph as ox
from rdflib import Literal
from rdflib.namespace import XSD
from rdflib.plugins.sparql.parserutils import CompValue

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope, _result
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.rdf.queries.syntax import prepare_local_query

from .native import native_term, qualify_literal, rdf_term
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


def query_native(
    request: dict[str, Any], store: ox.Store, budget: Budget
) -> dict[str, object]:
    """Parse the same finite local query profile, then execute the maintained store."""
    query = prepare_local_query(request["query"], {}, budget)
    pending = [query.algebra]
    probe = ox.Store()
    while pending:
        budget.consume("work")
        budget.consume("states")
        value = pending.pop()
        if isinstance(value, Literal):
            encoded = native_term(value)
            assert isinstance(encoded, ox.Literal)
            qualify_literal(encoded, probe)
        elif isinstance(value, CompValue):
            if value.name == "Builtin_STRDT" or (
                value.name == "Function" and str(value.get("iri")).startswith(str(XSD))
            ):
                raise LiteralScopeUnsupported(
                    "Datatype constructors are outside the disk query lexical profile"
                )
            pending.extend(value.values())
        elif isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
    scope = request["scope"]
    options: dict[str, Any] = {}
    if scope == "default":
        options = {"default_graph": ox.DefaultGraph(), "named_graphs": []}
    elif scope == "named":
        name = request["graph"]
        graph = (
            ox.BlankNode(name["value"])
            if name["kind"] == "blank"
            else ox.NamedNode(name["value"])
        )
        options = {"default_graph": graph, "named_graphs": []}
    elif scope == "union":
        options = {"use_default_graph_as_union": True, "named_graphs": []}
    elif scope != "dataset":
        raise InputError("Unknown native query graph scope")
    result = store.query(request["query"], **options)
    if isinstance(result, ox.QueryBoolean):
        return {
            "status": "result",
            "kind": "ASK",
            "answer": bool(result),
            "complete": True,
        }
    if not isinstance(result, ox.QuerySolutions):
        raise InputError("Only ASK and SELECT are supported")
    variables = [value.value for value in result.variables]
    budget.consume("states", len(variables))
    rows: list[list[dict[str, object] | None]] = []
    complete, message = True, ""
    try:
        for solution in result:
            budget.consume("work")
            if len(rows) >= request["max_rows"]:
                raise LimitExceeded("SELECT result exceeds max_rows")
            budget.consume("states", len(variables))
            row = [
                term_data(rdf_term(value)) if value is not None else None
                for value in solution
            ]
            budget.consume("bytes", json_bound(row, budget))
            rows.append(row)
    except LimitExceeded as error:
        complete, message = False, str(error)
    return {
        "status": "result",
        "kind": "SELECT",
        "variables": variables,
        "rows": rows,
        "complete": complete,
        "message": message,
    }
