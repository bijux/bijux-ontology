"""Owned worker request preparation and finite asynchronous batch evidence."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from bijux_ontology._json import own_json
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.execution.executor import ExecutionResult
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report


@dataclass(frozen=True, init=False)
class PreparedWorkerCheck:
    """Capture one JSON-shaped request, without caching semantic preparation.

    This synchronous capture owns caller data. Each execution still uses the
    ordinary installed worker and fresh native checks. Python callbacks, mutable
    graphs and in-process prepared indexes do not cross the existing protocol.
    """

    operation: str
    _parameters_json: str
    request_bytes: int

    def __init__(
        self,
        operation: str,
        parameters: Mapping[str, object],
        *,
        limits: Limits | None = None,
    ) -> None:
        """Bound the finite mapping before capturing its independent JSON values."""
        selected = limits if limits is not None else Limits()
        if not isinstance(selected, Limits):
            raise InputError("Prepared worker checks require Limits")
        budget = Budget(selected)
        if not isinstance(operation, str) or not operation:
            raise InputError("A prepared check requires an operation name")
        bound_text(operation, budget)
        if not isinstance(parameters, Mapping):
            raise InputError("Prepared worker parameters require a finite mapping")
        count = len(parameters)
        budget.consume("states", count)
        budget.consume("bytes", 64 + 64 * count)
        captured: dict[str, object] = {}
        for key in parameters:
            budget.consume("work")
            if not isinstance(key, str):
                raise InputError("Worker parameter names require text")
            bound_text(key, budget)
            name = str(key)
            if name in captured or len(captured) >= count:
                raise InputError("Worker parameters changed during capture")
            captured[name] = parameters[key]
        if len(captured) != count:
            raise InputError("Worker parameters changed during capture")
        owned = own_json(
            captured, max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        encoded = json.dumps(
            owned, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        bound_text(encoded, budget)
        object.__setattr__(self, "operation", str(operation))
        object.__setattr__(self, "_parameters_json", encoded)
        object.__setattr__(
            self,
            "request_bytes",
            len(encoded.encode("utf-8")) + len(operation.encode("utf-8")),
        )

    @property
    def parameters(self) -> dict[str, object]:
        """Return independent JSON data; no caller mutations alter the request."""
        return cast(dict[str, object], json.loads(self._parameters_json))


@dataclass(frozen=True)
class AsyncBatchResult:
    """Input-aligned results, required pending slots and actual partial reports.

    None marks an unstarted request. Data is omitted when the collection budget
    cannot retain it; the corresponding raw worker report remains visible.
    """

    results: tuple[ExecutionResult | None, ...]
    report: Report
