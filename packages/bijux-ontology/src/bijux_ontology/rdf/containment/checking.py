"""Finite simple entailment with explicit physical scope and original-term witnesses."""

import json
import threading
import time
from dataclasses import asdict
from typing import cast

from rdflib import URIRef

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import Budget, BudgetKind, IssueCollector, Limits
from bijux_ontology.processes.transport import checkpoint
from bijux_ontology.rdf.patches.codecs import read_term, witness_data
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.patches.types import PatchBlank, PatchLiteral, PatchUnavailable
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import literal_term

from .types import ContainmentCheck, TemplateBinding

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "states",
    "resources",
    "triples",
    "work",
    "issues",
)


def _run(
    data: Captured, template: Captured, operation: Operation
) -> tuple[bool, tuple[TemplateBinding, ...]]:
    """Reserve both handoff sides and accept only a complete owned worker receipt."""
    request = {
        "data": witness_data(data.witness, operation),
        "template": witness_data(template.witness, operation),
    }
    envelope: dict[str, object] = {
        "request": request,
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in _KINDS},
    }
    size = json_bound(envelope, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Containment request exceeds max_bytes")
    operation.consume("bytes", 2 * size)
    operation.consume("work", size)
    cap = min(
        operation.limits.max_bytes,
        operation.remaining("bytes") // 64,
        operation.remaining("work") // 8,
    )
    if cap < max(1024, size):
        raise LimitExceeded("Containment has no reserved response capacity")
    operation.consume("bytes", 32 * cap)
    operation.consume("work", 4 * cap)
    allowances = {kind: operation.remaining(kind) // 2 for kind in _KINDS}
    envelope["allowances"] = allowances
    payload = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    checkpoint(operation.deadline, operation.cancel)
    valid_receipt = False
    try:
        raw = execute_worker(
            "bijux_ontology.rdf.containment.worker",
            payload,
            cap=cap,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=operation.cancel,
        )
        response = object_value(
            decode_json(raw, max_bytes=cap, max_depth=operation.limits.max_depth),
            location="containment reply",
        )
        if set(response) != {"status", "data", "message", "controls"}:
            raise ResourceError("Malformed containment completion")
        controls = object_value(response["controls"], location="worker receipt")
        if set(controls) != set(_KINDS) or any(
            type(controls[kind]) is not int
            or not 0 <= cast(int, controls[kind]) <= allowances[kind]
            for kind in _KINDS
        ):
            raise ResourceError("Malformed containment worker receipt")
        for kind in _KINDS:
            operation.shared.consume(kind, cast(int, controls[kind]))
        valid_receipt = True
    finally:
        if not valid_receipt:
            # Missing receipts cannot establish that delegated work was unused.
            for kind in _KINDS:
                operation.shared.consume(kind, allowances[kind])
    checkpoint(operation.deadline, operation.cancel)
    if response["status"] == "exhausted":
        raise LimitExceeded("Native containment exhausted its owned allowance")
    if response["status"] != "ok":
        raise ResourceError("Native containment could not establish a complete answer")
    answer = object_value(response["data"], location="containment answer")
    entailed, rows = answer.get("entailed"), answer.get("bindings")
    if (
        set(answer) != {"entailed", "bindings"}
        or type(entailed) is not bool
        or not isinstance(rows, list)
    ):
        raise ResourceError("Malformed native containment answer")
    operation.consume("states", len(template.owned) + len(data.owned) + len(rows))
    operation.consume(
        "bytes", 128 * (len(template.owned) + len(data.owned) + len(rows))
    )
    template_nodes = {
        reference.token: node for node, reference in template.owned.values()
    }
    data_nodes = {reference.token: node for node, reference in data.owned.values()}
    retained: dict[str, TemplateBinding] = {}
    for row in rows:
        operation.consume("work")
        if (
            not isinstance(row, list)
            or len(row) != 2
            or type(row[0]) is not str
            or row[0] not in template_nodes
            or row[0] in retained
        ):
            raise ResourceError(
                "Native containment returned an unowned template variable"
            )
        value = read_term(row[1], operation)
        if isinstance(value, PatchBlank):
            if value.token not in data_nodes:
                raise ResourceError("Native containment returned an unowned data blank")
            term: QueryTerm = data_nodes[value.token]
        elif isinstance(value, URIRef):
            term = value
        elif isinstance(value, PatchLiteral):
            term = literal_term(
                value.lexical,
                datatype=None if value.language else value.datatype,
                language=value.language,
            )
        else:
            raise ResourceError("Native containment returned no RDF witness term")
        retained[row[0]] = TemplateBinding(template_nodes[row[0]], term)
    if (
        entailed
        and retained.keys() != template_nodes.keys()
        or not entailed
        and retained
    ):
        raise ResourceError("Native containment omitted coherent substitution evidence")
    return entailed, tuple(retained.values())


def check_simple_entailment(
    data: PreparedSource,
    template: PreparedSource,
    *,
    data_graph: GraphSelection | None = None,
    template_graph: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: threading.Event | None = None,
) -> ContainmentCheck:
    """Check a supplied graph template by maintained, inference-free BGP evaluation.

    Template blank nodes are existential variables. Repeated occurrences share
    one binding; distinct variables may coalesce. Ground RDF terms retain exact
    lexical identity, and extra data triples are allowed. An empty template is
    mathematically entailed, without establishing any nonempty coverage claim.
    Only complete selected physical graphs qualify. Native search internals are
    bounded by the owned deadline; counters cover explicit adapter work only.
    """
    if not isinstance(data, PreparedSource) or not isinstance(template, PreparedSource):
        raise InputError("Simple entailment requires owned PreparedSource inputs")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    entailed = None
    bindings: tuple[TemplateBinding, ...] = ()
    try:
        first = capture_source(
            data, "d", operation, graph_name=data_graph, project_graph=True
        )
        second = capture_source(
            template, "t", operation, graph_name=template_graph, project_graph=True
        )
        entailed, bindings = _run(first, second, operation)
        if not entailed:
            issues.append(
                Issue(
                    "rdf.template_not_entailed",
                    "No coherent substitution satisfies the selected graph template",
                    kind="finding",
                )
            )
    except LimitExceeded as error:
        entailed, bindings = None, ()
        issues.exhausted(str(error))
    except InputError as error:
        entailed, bindings = None, ()
        try:
            issues.append(
                Issue(
                    "rdf.containment_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | PatchUnavailable)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    complete = entailed is not None
    return ContainmentCheck(
        data,
        template,
        data_graph,
        template_graph,
        entailed,
        bindings,
        Report(
            "rdf.simple_entailment",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime="rdf-simple-entailment",
        ),
    )
