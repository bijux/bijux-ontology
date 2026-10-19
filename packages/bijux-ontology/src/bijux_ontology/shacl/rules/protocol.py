"""Bounded rule envelopes and cumulative worker accounting qualification."""

import json
import time
from dataclasses import asdict
from typing import TypedDict, cast

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, BudgetKind, Limits
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.rdf.patches.codecs import witness_data
from bijux_ontology.rdf.patches.types import SnapshotWitness

from .preparation import RuleUnavailable
from .types import RuleMode


class RuleRequest(TypedDict):
    """Exact wire fields; native preparation qualifies their semantic contents."""

    data: object
    rules: object
    namespaces: object
    selected: object
    mode: object
    maximum: object
    functions: object
    function_names: object
    function_namespaces: object
    limits: dict[str, int]
    allowances: dict[BudgetKind, int]


_STATUSES = ("result", "exhausted", "unsupported", "unavailable", "backend_error")


def run_rules(
    *,
    data: object,
    rules: object,
    namespaces: list[list[str]],
    selected: list[str],
    mode: RuleMode,
    maximum: int,
    functions: object,
    function_names: list[str],
    function_namespaces: list[list[str]],
    operation: Operation,
) -> dict[str, object]:
    """Adopt exact owned worker accounting before accepting any result metadata."""
    request: RuleRequest = {
        "data": data,
        "rules": rules,
        "namespaces": namespaces,
        "selected": selected,
        "mode": mode,
        "maximum": maximum,
        "functions": functions,
        "function_names": function_names,
        "function_namespaces": function_namespaces,
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in BUDGET_KINDS},
    }
    size = json_bound(request, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Rule request exceeds max_bytes")
    operation.consume("bytes", size)
    allowances = {kind: operation.remaining(kind) for kind in BUDGET_KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    operation.consume("work")
    raw = execute_worker(
        "bijux_ontology.shacl.rules.worker",
        payload,
        cap=operation.limits.max_bytes,
        timeout=operation.deadline - time.monotonic(),
        cancel=operation.cancel,
    )
    operation.consume("bytes", len(raw))
    value = decode_json(raw, max_bytes=operation.limits.max_bytes)
    if not isinstance(value, dict) or value.get("status") not in _STATUSES:
        raise ResourceError("Malformed rule worker response")
    consumed = value.get("consumed")
    if (
        not isinstance(consumed, dict)
        or set(consumed) != set(BUDGET_KINDS)
        or any(
            type(consumed[kind]) is not int
            or not 0 <= consumed[kind] <= allowances[kind]
            for kind in BUDGET_KINDS
        )
    ):
        raise ResourceError("Malformed rule worker accounting")
    for kind in BUDGET_KINDS:
        operation.consume(kind, consumed[kind])
    status = value["status"]
    if status != "result":
        if set(value) != {"status", "consumed"}:
            raise ResourceError("Malformed incomplete rule completion")
        if status == "exhausted":
            raise LimitExceeded("Rule materialization exhausted its declared allowance")
        if status == "unavailable":
            raise RuleUnavailable(
                "Install bijux-ontology[shacl] for maintained rule execution"
            )
        if status == "unsupported":
            raise RuleUnavailable(
                "Required rule semantics exceed the finite supported profile"
            )
        raise RuleUnavailable(
            "The maintained rule executor did not complete successfully"
        )
    if set(value) != {"status", "consumed", "passes", "effective"}:
        raise ResourceError("Malformed successful rule completion")
    return value


def read_request(raw: bytes, cap: int) -> tuple[RuleRequest, ControlBudget]:
    """Require the shared envelope before constructing native worker controls."""
    request = decode_json(raw, max_bytes=cap)
    if (
        not isinstance(request, dict)
        or set(request) != RuleRequest.__required_keys__
        or not isinstance(request["limits"], dict)
    ):
        raise InputError("Malformed rule worker request")
    typed = cast(RuleRequest, request)
    return typed, ControlBudget(Limits(**typed["limits"]), typed["allowances"])


def failed_execution(error: Exception) -> dict[str, object]:
    """Classify native failure without retaining caller-influenced backend text."""
    if isinstance(error, LimitExceeded | MemoryError):
        status = "exhausted"
    elif isinstance(error, ImportError):
        status = "unavailable"
    elif isinstance(error, RuleUnavailable):
        status = "unsupported"
    else:
        status = "backend_error"
    return {"status": status}


def completed_execution(
    passes: int, effective: SnapshotWitness, budget: Budget
) -> dict[str, object]:
    """Encode native completion without transferring semantic acceptance to wire IO."""
    return {
        "status": "result",
        "passes": passes,
        "effective": witness_data(effective, budget),
    }


def response_bytes(result: dict[str, object], budget: ControlBudget, cap: int) -> bytes:
    """Serialize the exact completion envelope after its original owned charges."""
    try:
        size = json_bound(result, budget)
        if size > cap:
            raise LimitExceeded("Rule response exceeds max_bytes")
        budget.consume("bytes", size)
    except LimitExceeded:
        result = {"status": "exhausted"}
    result["consumed"] = budget.consumed()
    output = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    if len(output) > cap:
        raise InputError("Rule response exceeds its output cap")
    return output
