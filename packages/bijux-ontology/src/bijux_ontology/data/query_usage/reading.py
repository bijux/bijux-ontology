"""Own bounded program inputs and delegate parsing to one concrete local worker."""

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import asdict
from threading import Event
from typing import Literal, cast

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, report_from_data
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import (
    AlgebraPosition,
    QueryDynamicScope,
    QueryReference,
    QueryRole,
    QueryUsage,
)

KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)
ROLES = frozenset(("class", "predicate", "datatype", "data", "function", "graph"))


def reserve(budget: Budget, count: int = 1) -> None:
    """Prepay bounded traversal and immutable/container reference ownership."""
    budget.consume("work", count)
    budget.consume("states", count)
    budget.consume("bytes", 96 * count)


def position(value: object) -> AlgebraPosition:
    """Read concrete algebra keys and nonnegative indices from a bounded peer."""
    if type(value) is not list or any(
        type(part) not in (str, int) or type(part) is int and part < 0 for part in value
    ):
        raise InputError("Malformed query algebra position")
    return tuple(value)


def _adopt(
    raw: bytes, operation: Operation, allowances: dict[BudgetKind, int], cap: int
) -> tuple[tuple[QueryReference, ...], tuple[QueryDynamicScope, ...], Report]:
    data = object_value(
        decode_json(raw, max_bytes=cap, max_depth=operation.limits.max_depth),
        location="query usage response",
    )
    if set(data) != {"references", "dynamic", "report", "consumed"}:
        raise InputError("Malformed query usage response fields")
    receipt = object_value(data["consumed"], location="query usage receipt")
    if set(receipt) != set(KINDS):
        raise InputError("Malformed query usage control receipt")
    for kind in KINDS:
        count = receipt[kind]
        if type(count) is not int or not 0 <= count <= allowances[kind]:
            raise InputError("Query usage worker exceeded its shared allowance")
    for kind in KINDS:
        operation.consume(kind, cast(int, receipt[kind]))
    report = report_from_data(data["report"])
    operation.shared._register_transferred_report(
        report, paid_issues=cast(int, receipt["issues"]), max_references=2 * cap + 1
    )
    references = []
    dynamic = []
    if type(data["references"]) is not list or type(data["dynamic"]) is not list:
        raise InputError("Malformed query usage inventories")
    for item in data["references"]:
        ref = object_value(item, location="query reference")
        if set(ref) != {
            "iri",
            "role",
            "position",
            "location",
            "binding_position",
            "reverse",
        } or (
            type(ref["iri"]) is not str
            or type(ref["role"]) is not str
            or ref["role"] not in ROLES
            or type(ref["location"]) is not str
            or type(ref["reverse"]) is not bool
        ):
            raise InputError("Malformed query reference fields")
        absolute_iri(ref["iri"])
        references.append(
            QueryReference(
                ref["iri"],
                cast(QueryRole, ref["role"]),
                position(ref["position"]),
                ref["location"],
                None
                if ref["binding_position"] is None
                else position(ref["binding_position"]),
                ref["reverse"],
            )
        )
    for item in data["dynamic"]:
        scope = object_value(item, location="query dynamic scope")
        if set(scope) != {"role", "position", "reason", "variable"} or (
            type(scope["role"]) is not str
            or type(scope["reason"]) is not str
            or scope["variable"] is not None
            and type(scope["variable"]) is not str
        ):
            raise InputError("Malformed query dynamic scope fields")
        dynamic.append(
            QueryDynamicScope(
                scope["role"],
                position(scope["position"]),
                scope["reason"],
                scope["variable"],
            )
        )
    return tuple(references), tuple(dynamic), report


def read_query_usage(
    text: str,
    *,
    kind: Literal["query", "update"] = "query",
    source: str = "",
    base: str | None = None,
    namespaces: Sequence[tuple[str, str]] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> QueryUsage:
    """Inventory finite static references without executing a query or update.

    Source locations identify maintained resolved algebra positions. They are
    not byte offsets, line numbers, query results or proofs of executed branches.
    Native parser computation is bounded by the owned process deadline; counters
    cover explicit input, traversal and output controls only.
    """
    operation = Operation(limits, budget, timeout, cancel)
    if (
        type(text) is not str
        or type(source) is not str
        or type(cast(object, kind)) is not str
        or cast(object, kind) not in ("query", "update")
    ):
        raise InputError(
            "Query usage requires ordinary text/source and an explicit program kind"
        )
    if base is not None and type(base) is not str:
        raise InputError("Query usage base must be ordinary absolute IRI text")
    issues = IssueCollector(operation)
    fingerprint = ""
    references: tuple[QueryReference, ...] = ()
    dynamic: tuple[QueryDynamicScope, ...] = ()
    try:
        for value in (text, source):
            bound_text(value, operation)
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
        captured = capture_items(namespaces, tuple, operation)
        bindings: dict[str, str] = {}
        for pair in captured:
            if len(pair) != 2 or any(type(part) is not str for part in pair):
                raise InputError(
                    "Namespace bindings require ordinary prefix/IRI text pairs"
                )
            name, iri = pair
            bound_text(name, operation)
            bound_text(iri, operation)
            absolute_iri(iri)
            reserve(operation)
            if name in bindings:
                raise InputError("Namespace bindings must have unique prefixes")
            bindings[name] = iri
        operation.consume("bytes", 4 * len(text) + 128)
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        envelope: dict[str, object] = {
            "text": text,
            "source": source,
            "kind": kind,
            "base": base,
            "namespaces": bindings,
            "limits": asdict(operation.limits),
            "allowances": {key: operation.remaining(key) for key in KINDS},
        }
        size = json_bound(envelope, operation)
        if size > operation.limits.max_bytes:
            raise LimitExceeded("Query usage input exceeds max_bytes")
        operation.consume("bytes", 2 * size)
        operation.consume("work", size)
        encoded = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        cap = min(operation.limits.max_bytes, max(8192, 32 * len(text) + len(encoded)))
        reserve(operation, 2 * cap + 1)
        operation.consume("bytes", 6 * cap)
        allowances = {key: operation.remaining(key) for key in KINDS}
        envelope["allowances"] = allowances
        encoded = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("Query usage exceeded its wall-clock deadline")
        try:
            raw = execute_worker(
                "bijux_ontology.data.query_usage.worker",
                encoded,
                cap=cap,
                timeout=remaining,
                cancel=cancel,
            )
            references, dynamic, report = _adopt(raw, operation, allowances, cap)
        except InputError as error:
            for key in KINDS:
                operation.shared.consume(key, operation.shared.remaining(key))
            raise LimitExceeded(
                "Query usage lacks a qualified worker control receipt; "
                "delegated allowances were forfeited: " + str(error)
            ) from error
        return QueryUsage._owned(source, kind, fingerprint, references, dynamic, report)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("query.usage_input", str(error), source, kind="input_error")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return QueryUsage._owned(
        source,
        kind,
        fingerprint,
        references,
        dynamic,
        Report(
            "query.inventory",
            issues=tuple(issues),
            requested=1,
            complete=False,
            families=("query.static_dependencies",),
            regime="sparql-1.1-algebra",
        ),
    )
