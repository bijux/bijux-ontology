"""Bounded native markup evidence with explicit host-adoption reservation."""

import json
import time
from dataclasses import asdict
from typing import Any

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, bound_text
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.receipts import TerminalResponse


def run_markup(
    text: str, kind: str, canonicalization: str | None, budget: Operation
) -> dict[str, Any]:
    """Reserve host adoption before offering native work its actual allowance.

    Half the remaining control work/states/bytes stays on the host; the worker
    receives the other half, and output has an additional explicit byte cap.
    Counters cover our actual capture and traversal, while maintained native
    internals are bounded by parser safety limits and the owned-process deadline.
    """
    request: dict[str, Any] = {
        "text": text,
        "kind": kind,
        "canonicalization": canonicalization,
        "limits": asdict(budget.limits),
        "allowances": {key: budget.remaining(key) for key in BUDGET_KINDS},
        "output_cap": budget.limits.max_total_bytes,
    }
    budget.consume("bytes", json_bound(request, budget))
    maximum = min(budget.limits.max_total_bytes, budget.remaining("bytes") // 4)
    terminal = TerminalResponse(budget, maximum)
    allowances = {
        key: terminal.available(key) // 2
        if key in ("bytes", "states", "work")
        else terminal.available(key)
        for key in BUDGET_KINDS
    }
    # The native evaluator returns data/counters; host adoption owns Issues.
    allowances["issues"] = 0
    delegated = DelegatedAllowance(budget, allowances)
    request["allowances"] = delegated.to_data()
    request["output_cap"] = terminal.cap
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    dispatched = received = False
    try:
        terminal.admit(payload)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.rdf.literal_values.markup.worker",
            payload,
            cap=terminal.cap,
            timeout=max(0.000001, budget.deadline - time.monotonic()),
            cancel=budget.cancel,
        )
        response = terminal.decode(raw)
        if not isinstance(response, dict) or response.get("status") not in (
            "result",
            "exhausted",
            "unavailable",
            "unverified",
            "input_error",
        ):
            raise ResourceError("Invalid native markup result")
        delegated.settle(response.get("consumed"))
        received = True
    finally:
        try:
            terminal.close()
        finally:
            if dispatched and not received:
                delegated.forfeit()
    budget.consume("work", 0)
    if response["status"] == "exhausted":
        raise LimitExceeded("Native markup did not finish within its allowance")
    backend = response.get("backend", "")
    if not isinstance(backend, str):
        raise ResourceError("Invalid native markup backend identity")
    bound_text(backend, budget)
    if response["status"] != "result":
        return response
    names, text_value, comments = (
        response.get("names"),
        response.get("text"),
        response.get("comments"),
    )
    if (
        not isinstance(names, list)
        or not isinstance(text_value, str)
        or type(comments) is not int
        or not 0 <= comments <= budget.limits.max_states
    ):
        raise ResourceError("Invalid native markup observations")
    budget.consume("states", len(names))
    budget.consume("bytes", 64 + 8 * len(names))
    for name in names:
        if not isinstance(name, str):
            raise ResourceError("Invalid native markup element name")
        bound_text(name, budget)
    bound_text(text_value, budget)
    canonical = response.get("canonical")
    if (canonicalization is not None) != (canonical is not None):
        raise ResourceError("Native markup omitted its selected canonical role")
    if canonical is not None:
        if not isinstance(canonical, str):
            raise ResourceError("Invalid native canonical markup bytes")
        bound_text(canonical, budget)
        budget.consume("bytes", len(canonical.encode("utf-8")))
    return response
