"""Owned answer capture and cumulative bounded native parser handoff."""

import json
import time
from dataclasses import asdict
from threading import Event
from typing import cast

from rdflib import BNode

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.queries.access import QueryResult
from bijux_ontology.rdf.queries.protocol import QueryTerm, read_term, term_data
from bijux_ontology.rdf.terms import bound_node

from .types import AnswerArtifact, AnswerFormat, AnswerUnavailable

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def _capture(
    data: dict[str, object], operation: Budget, external_complete: bool | None
) -> AnswerArtifact:
    """Qualify one internal typed table before exposing its owned term inventory."""
    if external_complete is not None and type(external_complete) is not bool:
        raise InputError("External completeness requires bool or None")
    if set(data) != {"kind", "answer", "variables", "rows"}:
        raise InputError("Malformed typed answer fields")
    kind, answer, variables, supplied = (
        data[k] for k in ("kind", "answer", "variables", "rows")
    )
    if (
        kind not in ("ASK", "SELECT")
        or not isinstance(variables, (list, tuple))
        or not isinstance(supplied, (list, tuple))
    ):
        raise InputError("Malformed typed answer structure")
    seen: set[str] = set()
    for name in variables:
        if type(name) is not str or not name:
            raise InputError("Answer variables require nonempty names")
        bound_text(name, operation)
        operation.consume("states")
        if name in seen:
            raise InputError("Duplicate answer variable")
        seen.add(name)
    if kind == "ASK" and (type(answer) is not bool or variables or supplied):
        raise InputError("ASK requires only a boolean answer")
    if kind == "SELECT" and answer is not None:
        raise InputError("SELECT cannot have a boolean answer")
    blanks: dict[str, BNode] = {}
    rows: list[tuple[QueryTerm | None, ...]] = []
    for raw in supplied:
        if not isinstance(raw, (tuple, list)) or len(raw) != len(variables):
            raise InputError("Answer row width differs from its variables")
        operation.consume("states", len(raw) + 1)
        operation.consume("bytes", 128 * (len(raw) + 1))
        row = []
        for cell in raw:
            operation.consume("work")
            if isinstance(cell, dict):
                for value in cell.values():
                    if isinstance(value, str):
                        bound_text(value, operation)
            if isinstance(cell, dict):
                operation.consume(
                    "bytes",
                    32
                    * (
                        64
                        + sum(
                            len(value)
                            for value in cell.values()
                            if isinstance(value, str)
                        )
                    ),
                )
            term = read_term(cell, blanks)
            if term is not None:
                bound_node(term, operation)
            row.append(term)
        rows.append(tuple(row))
    return AnswerArtifact._new(
        kind,
        cast(bool | None, answer),
        tuple(variables),
        tuple(rows),
        external_complete,
        Report("rdf.answers.read", 1, requested=1),
    )


def _failure(issues: IssueCollector, external_complete: bool | None) -> AnswerArtifact:
    """A stopped or invalid parser never exposes an apparently empty full table."""
    return AnswerArtifact._new(
        None,
        None,
        (),
        (),
        external_complete,
        Report("rdf.answers.read", requested=1, complete=False, issues=tuple(issues)),
    )


def capture_answer(
    result: QueryResult,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> AnswerArtifact:
    """Own existing local result terms; preserve incomplete execution as incomplete."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    try:
        if type(result) is not QueryResult:
            raise InputError("Expected a typed local QueryResult")
        if not result.report.verified:
            issues.append(
                Issue(
                    "rdf.answer_source_unverified",
                    "Local query did not establish a complete answer",
                    kind="unverified",
                )
            )
            return _failure(issues, False)
        rows = []
        for row in result.rows:
            operation.consume("states", len(row) + 1)
            operation.consume("bytes", 128 * (len(row) + 1))
            encoded = []
            for term in row:
                if term is not None:
                    bound_node(term, operation)
                encoded.append(term_data(term))
            rows.append(encoded)
        return _capture(
            {
                "kind": result.kind,
                "answer": result.answer,
                "variables": result.variables,
                "rows": rows,
            },
            operation,
            True,
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("rdf.answer_input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return _failure(issues, None)


def read_answers(
    payload: str | bytes,
    *,
    format: AnswerFormat,
    external_complete: bool | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> AnswerArtifact:
    """Read UTF-8 bytes or text; validity proves no external query completeness.

    Actual parser execution has an owned hard deadline. Missing control receipts
    forfeit the remaining delegated allowance before returning; retries cannot
    recover unknown native work. CSV cannot recover RDF kinds or unbound identity.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    try:
        if not isinstance(cast(object, format), str) or format not in {
            "json",
            "xml",
            "tsv",
            "csv",
        }:
            raise InputError("Unsupported answer format")
        if external_complete is not None and type(external_complete) is not bool:
            raise InputError("External completeness requires bool or None")
        if format == "csv":
            issues.append(
                Issue(
                    "rdf.answer_identity_lost",
                    "CSV cannot establish RDF term and unbound identity",
                    kind="unverified",
                )
            )
            return _failure(issues, external_complete)
        if not isinstance(payload, (str, bytes)):
            raise InputError("Answer payload requires text or bytes")
        if len(payload) > operation.limits.max_bytes:
            raise LimitExceeded("Answer input exceeds max_bytes")
        operation.consume("bytes", len(payload) * 4 + 64)
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        bound_text(text, operation)
        data = _dispatch(text, format, operation)
        return _capture(
            object_value(data, location="answer"), operation, external_complete
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, UnicodeError) as error:
        try:
            issues.append(Issue("rdf.answer_input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return _failure(issues, external_complete)


def _dispatch(
    text: str, format: str, operation: Operation, *, mode: str = "parse"
) -> object:
    """Share one precharged fixed native codec handoff for reads and writes."""
    envelope: dict[str, object] = {
        "payload": text,
        "format": format,
        "mode": mode,
        "limits": asdict(operation.limits),
        "allowances": {k: operation.remaining(k) for k in _KINDS},
        "cap": min(operation.limits.max_bytes, len(text) * 16 + 4096),
    }
    size = json_bound(envelope, operation)
    operation.consume("bytes", 2 * size)
    cap = cast(int, envelope["cap"])
    # Pay retained peer bytes, decoded objects and term references before
    # delegating residual counters to opaque maintained native parser code.
    operation.consume("bytes", 64 * cap)
    operation.consume("states", 2 * cap + 16)
    operation.consume("work", 4 * cap)
    allowances = {k: operation.remaining(k) for k in _KINDS}
    envelope["allowances"] = allowances
    raw_request = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    try:
        raw = execute_worker(
            "bijux_ontology.rdf.answers.worker",
            raw_request,
            cap=cap,
            timeout=operation.deadline - time.monotonic(),
            cancel=operation.cancel,
        )
        response = object_value(decode_json(raw, max_bytes=cap), location="worker")
        if set(response) != {"status", "data", "message", "controls"}:
            raise InputError("Malformed answer worker receipt")
        controls = response["controls"]
        if not isinstance(controls, dict) or set(controls) != set(_KINDS):
            raise InputError("Missing answer worker counters")
        for kind in _KINDS:
            amount = controls[kind]
            if type(amount) is not int or not 0 <= amount <= allowances[kind]:
                raise InputError("Invalid answer worker counter")
        for kind in _KINDS:
            operation.consume_completed(kind, controls[kind])
    except InputError as error:
        for kind in _KINDS:
            operation.consume_completed(kind, operation.remaining(kind))
        raise LimitExceeded(
            "Answer parser stopped without a qualified control receipt: " + str(error)
        ) from error
    if response["status"] == "exhausted":
        raise LimitExceeded(str(response["message"]))
    if response["status"] == "unverified":
        raise AnswerUnavailable(str(response["message"]))
    if response["status"] == "input_error":
        raise InputError(str(response["message"]))
    if response["status"] != "ok":
        raise InputError("Unknown answer worker status")
    return response["data"]
