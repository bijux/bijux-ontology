"""Bounded consumer obligations over asserted local query truth."""

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from math import isfinite
from typing import cast

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report, combine_reports
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.execution.processes.transport import checkpoint
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope, query_rdf
from bijux_ontology.rdf.queries.protocol import QueryTerm, term_data
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import term_key

from .types import QueryAnswers, QueryRule, QueryRuleResult


def _binding_issue(
    rule: QueryRule,
    variables: tuple[str, ...],
    row: tuple[QueryTerm | None, ...],
    code: str,
    message: str,
    budget: Budget,
) -> Issue:
    """Retain exact failing RDF bindings and established file/term scope."""
    bindings = {
        name: term_data(term) for name, term in zip(variables, row, strict=True)
    }
    evidence = json.dumps({"rule": rule.name, "bindings": bindings}, ensure_ascii=True)
    budget.consume("bytes", len(evidence.encode()))
    first = next((term for term in row if isinstance(term, URIRef)), None)
    return Issue(
        code,
        message,
        rule.name,
        actual=json.dumps(bindings, ensure_ascii=True),
        details=Location(
            source=rule.source,
            term=str(first) if first is not None else None,
            graph=str(rule.scope.graph_name)
            if rule.scope.graph_name is not None
            else None,
        ),
        evidence_json=evidence,
    )


def _rows(
    rows: tuple[tuple[QueryTerm | None, ...], ...],
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> dict[tuple[tuple[str | None, ...], ...], tuple[QueryTerm | None, ...]]:
    """Compare finite solution sets by RDF identity, never literal value equality."""
    result: dict[tuple[tuple[str | None, ...], ...], tuple[QueryTerm | None, ...]] = {}
    for row in rows:
        checkpoint(deadline, cancel)
        budget.consume("work")
        budget.consume("states", max(1, len(row)))
        result.setdefault(tuple(term_key(term) for term in row), row)
    return result


def _retain_rows(
    variables: tuple[str, ...],
    rows: tuple[tuple[QueryTerm | None, ...], ...],
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> None:
    """Bound cumulative expected/raw answers, including rows without findings."""
    budget.consume("states", len(variables))
    budget.consume("bytes", len(json.dumps(variables).encode()))
    for row in rows:
        checkpoint(deadline, cancel)
        budget.consume("states", max(1, len(row)))
        values = [term_data(term) for term in row]
        if any(
            isinstance(text, str) and len(text) > budget.limits.max_bytes
            for value in values
            if value is not None
            for text in value.values()
        ):
            raise LimitExceeded("Query answer term exceeds max_bytes")
        encoded = json.dumps(values, ensure_ascii=True).encode()
        if len(encoded) > budget.limits.max_bytes:
            raise LimitExceeded("Query answer row exceeds max_bytes")
        budget.consume("bytes", len(encoded))


def _assess(
    rule: QueryRule,
    result: QueryResult,
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> Report:
    """Preserve engine failures and distinguish actual answer disagreements."""
    issues = IssueCollector(budget)
    complete = result.report.complete
    try:
        issues.extend(result.report.issues)
        expected = rule.expected
        wanted = "ASK" if type(expected) is bool else "SELECT"
        if result.kind is not None and result.kind != wanted:
            issues.append(
                Issue(
                    "data.query_kind_mismatch",
                    "Query form differs from its declared answer contract",
                    rule.name,
                    kind="input_error",
                    expected=wanted,
                    actual=result.kind,
                )
            )
        elif result.kind == "ASK" and result.report.verified:
            if result.answer is not expected:
                issues.append(
                    Issue(
                        "data.query_answer_mismatch",
                        "Asserted ASK answer differs from the expected answer",
                        rule.name,
                        expected=str(expected),
                        actual=str(result.answer),
                        details=Location(source=rule.source),
                    )
                )
        elif result.kind == "SELECT" and (
            result.report.verified or result.report.outcome == "exhausted"
        ):
            actual = _rows(result.rows, budget, deadline, cancel)
            if isinstance(expected, QueryAnswers):
                if result.variables != expected.variables:
                    issues.append(
                        Issue(
                            "data.query_variables_mismatch",
                            "SELECT variables differ from the expected answer schema",
                            rule.name,
                            expected=str(expected.variables),
                            actual=str(result.variables),
                        )
                    )
                else:
                    declared = _rows(expected.rows, budget, deadline, cancel)
                    for key in actual.keys() - declared.keys():
                        checkpoint(deadline, cancel)
                        issues.append(
                            _binding_issue(
                                rule,
                                result.variables,
                                actual[key],
                                "data.query_answer_unexpected",
                                "SELECT returned an unexpected exact answer",
                                budget,
                            )
                        )
                    if result.report.complete:
                        for key in declared.keys() - actual.keys():
                            checkpoint(deadline, cancel)
                            issues.append(
                                _binding_issue(
                                    rule,
                                    expected.variables,
                                    declared[key],
                                    "data.query_answer_missing",
                                    "SELECT did not return an expected exact answer",
                                    budget,
                                )
                            )
            else:
                for row in actual.values():
                    checkpoint(deadline, cancel)
                    issues.append(
                        _binding_issue(
                            rule,
                            result.variables,
                            row,
                            "data.query_violation",
                            "SELECT returned a violating binding",
                            budget,
                        )
                    )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=rule.name)
        complete = False
    return replace(
        result.report,
        scope=rule.name,
        issues=tuple(issues),
        complete=complete,
        regime="asserted-sparql",
        families=("query-rule",),
    )


def _query(
    source: PreparedSource,
    text: str,
    scope: QueryScope,
    limits: Limits,
    max_rows: int,
    deadline: float,
    cancel: threading.Event | None,
    owned_worker: bool,
) -> QueryResult:
    """Keep each maintained engine call inside the suite's absolute deadline."""
    checkpoint(deadline, cancel)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Query rule suite exceeded its wall-clock deadline")
    result = query_rdf(
        source,
        text,
        scope=scope,
        max_rows=max_rows,
        limits=limits,
        timeout=remaining,
        cancel=cancel,
        _owned_worker=owned_worker,
    )
    checkpoint(deadline, cancel)
    return result


def _nonempty_query(scope: QueryScope) -> str:
    """Inspect only the chosen dataset/graph, including selected named graph data."""
    return (
        "ASK {{?s ?p ?o} UNION {GRAPH ?g {?s ?p ?o}}}"
        if scope.kind == "dataset"
        else "ASK {?s ?p ?o}"
    )


def run_query_rules(
    source: PreparedSource,
    rules: Sequence[QueryRule],
    *,
    limits: Limits | None = None,
    max_rows: int = 1000,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
    allow_empty: bool = False,
    _owned_worker: bool = False,
) -> QueryRuleResult:
    """Run finite named ASK/SELECT obligations on one owned prepared resource.

    Rules retain raw query answers and independent expectation diagnostics. A
    nonempty selected data scope is required unless allow_empty explicitly
    excludes that obligation. An empty required rule inventory never verifies.
    The suite has one absolute deadline and cumulative declaration/result/issue
    retention limits. Each query engine call retains its own bounded capture,
    parser/evaluation and reply stages; opaque search is bounded by real workers.
    These rules concern supplied asserted RDF, never implicit OWL entailment.
    """
    if not isinstance(source, PreparedSource):
        raise InputError("Query rules require an owned PreparedSource")
    if not isinstance(rules, Sequence) or isinstance(cast(object, rules), str | bytes):
        raise InputError("Query rules require a finite selected sequence")
    if type(max_rows) is not int or max_rows <= 0:
        raise InputError("Query rule max_rows must be a positive integer")
    if type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
        raise InputError("Query rule timeout must be finite and positive")
    if type(allow_empty) is not bool:
        raise InputError("allow_empty must be a boolean")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Query rule cancellation requires a threading.Event")
    selected = limits if limits is not None else Limits()
    deadline = time.monotonic() + timeout
    budget = Budget(selected)
    required: int | None = None
    captured: list[QueryRule] = []
    try:
        checkpoint(deadline, cancel)
        required = len(rules)
        checkpoint(deadline, cancel)
        for index in range(min(required, selected.max_resources)):
            checkpoint(deadline, cancel)
            budget.consume("work")
            budget.consume("states")
            captured.append(rules[index])
            checkpoint(deadline, cancel)
    except LimitExceeded as error:
        pending_message = (
            f"{required} required query rules were not started"
            if required is not None and required > 0
            else "Required query rules were not started; inventory was not captured"
        )
        return QueryRuleResult(
            Report(
                "query-rules",
                requested=max(1, required or 0),
                complete=False,
                issues=(
                    Issue(
                        "execution.limit_exceeded",
                        f"{error}; {pending_message}",
                        "query-rules.remaining",
                        kind="exhausted",
                    ),
                ),
                regime="asserted-sparql",
                exclusions=("empty-data-selection",) if allow_empty else (),
            )
        )
    if not required:
        return QueryRuleResult(
            Report(
                "query-rules",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "input.empty_scope",
                        "Required query rule inventory is empty",
                        kind="input_error",
                    ),
                ),
            )
        )
    if any(not isinstance(rule, QueryRule) for rule in captured):
        raise InputError("Expected typed QueryRule declarations")
    if len({rule.name for rule in captured}) != len(captured):
        raise InputError("Query rule names must be unique")
    guards: dict[QueryScope, QueryResult] = {}
    reports: list[Report] = []
    results = []
    for rule in captured:
        try:
            checkpoint(deadline, cancel)
            budget.consume("resources")
            budget.consume("work")
            if len(rule.query) > selected.max_bytes:
                raise LimitExceeded("Query rule text exceeds max_bytes")
            budget.consume(
                "bytes",
                len(rule.name.encode())
                + len(rule.query.encode())
                + len(rule.source.encode()),
            )
            if isinstance(rule.expected, QueryAnswers):
                _retain_rows(
                    rule.expected.variables,
                    rule.expected.rows,
                    budget,
                    deadline,
                    cancel,
                )
            guard = guards.get(rule.scope)
            if not allow_empty and guard is None:
                budget.consume("work")
                checkpoint(deadline, cancel)
                guard = _query(
                    source,
                    _nonempty_query(rule.scope),
                    rule.scope,
                    selected,
                    max_rows,
                    deadline,
                    cancel,
                    _owned_worker,
                )
                guards[rule.scope] = guard
            if guard is not None and not guard.report.verified:
                budget.consume("issues", len(guard.report.issues))
                reports.append(replace(guard.report, scope=rule.name))
            elif guard is not None and guard.answer is not True:
                budget.consume("issues")
                reports.append(
                    Report(
                        rule.name,
                        requested=1,
                        complete=False,
                        issues=(
                            Issue(
                                "data.query_empty_scope",
                                "Required query selection contains no RDF statements",
                                rule.name,
                                kind="input_error",
                                details=Location(source=rule.source),
                            ),
                        ),
                    )
                )
            else:
                checkpoint(deadline, cancel)
                result = _query(
                    source,
                    rule.query,
                    rule.scope,
                    selected,
                    max_rows,
                    deadline,
                    cancel,
                    _owned_worker,
                )
                _retain_rows(result.variables, result.rows, budget, deadline, cancel)
                results.append((rule.name, result))
                reports.append(_assess(rule, result, budget, deadline, cancel))
            if reports[-1].outcome == "exhausted":
                break
        except LimitExceeded as error:
            reports.append(
                Report(
                    rule.name,
                    requested=1,
                    complete=False,
                    issues=(
                        Issue(
                            "execution.limit_exceeded",
                            str(error),
                            rule.name,
                            kind="exhausted",
                        ),
                    ),
                )
            )
            break
    pending = required - len(reports)
    if pending:
        reports.append(
            Report(
                "query-rules.remaining",
                requested=pending,
                complete=False,
                issues=(
                    Issue(
                        "execution.limit_exceeded",
                        f"{pending} required query rules were not started",
                        "query-rules.remaining",
                        kind="exhausted",
                    ),
                ),
            )
        )
    report = combine_reports("query-rules", *reports)
    return QueryRuleResult(
        replace(
            report,
            regime="asserted-sparql",
            exclusions=("empty-data-selection",) if allow_empty else (),
        ),
        tuple(results),
    )
