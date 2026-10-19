"""Disjoint funded JSON receipt adoption after an owned native operation stops."""

from dataclasses import replace

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, BudgetKind
from bijux_ontology.json.values import decode_json


class TerminalResponse:
    """Own one finite host reservation without renewing semantic work permission.

    Construction splits one observed remaining inventory. The caller offers only
    available() capacity to its worker and performs no other parent-budget work
    until close(). This reservation owns transport and terminal JSON decoding;
    subsequent semantic adoption remains on the live enclosing operation.
    Actual local counters settle once, including failed decoding. Unused known
    host capacity is never charged; unknown native work belongs to its separate
    DelegatedAllowance. There is no fresh deadline or arbitrary work callback.
    """

    def __init__(
        self, owner: Operation, maximum: int, *, max_depth: int | None = None
    ) -> None:
        """Capture disjoint maxima before request admission or native execution."""
        if not isinstance(owner, Operation):
            raise InputError("Terminal responses require an owned Operation")
        if type(maximum) is not int or maximum <= 0:
            raise InputError("Terminal response maximum must be a positive integer")
        depth = owner.limits.max_depth if max_depth is None else max_depth
        if type(depth) is not int or depth <= 0:
            raise InputError("Terminal response depth must be a positive integer")
        inventory = {kind: owner.remaining(kind) for kind in BUDGET_KINDS}
        host = {
            kind: inventory[kind] // 2 if kind in ("bytes", "work", "states") else 0
            for kind in BUDGET_KINDS
        }
        # Eight byte copies cover bounded pipes and retained transport buffers;
        # the decoder's own 96-byte/source-byte prepayment remains within its
        # 128-byte/source-byte window. Actual traversal can exhaust this window.
        # Work admission funds the request, not a fictitious worst-case parse;
        # each terminal traversal step still spends the disjoint host allowance.
        cap = min(
            maximum,
            (host["bytes"] - 4096) // 136,
            host["work"] - 128,
        )
        if cap < 64 or host["states"] < 16:
            raise LimitExceeded("Insufficient funded terminal response capacity")
        self._cap = cap
        self._available = {kind: inventory[kind] - host[kind] for kind in BUDGET_KINDS}
        self._host = ControlBudget(
            replace(owner.limits, max_bytes=cap, max_depth=depth), host
        )
        self._host_costs = DelegatedAllowance(owner, host)
        self._admitted = self._decoded = self._closed = False

    @property
    def cap(self) -> int:
        """Expose the immutable transport/parser byte admission bound."""
        return self._cap

    def available(self, kind: BudgetKind) -> int:
        """Read worker capacity excluding every host reservation from this split."""
        return self._available[kind]

    def admit(self, payload: bytes) -> None:
        """Admit one already-owned request before starting its bounded transport."""
        if self._admitted or self._closed:
            raise InputError("Terminal response already admitted or closed")
        if type(payload) is not bytes:
            raise InputError("Terminal requests require immutable bytes")
        if len(payload) > self._cap:
            raise LimitExceeded("Request exceeds funded terminal transport capacity")
        self._host.consume("bytes", 4 * len(payload) + 512)
        self._host.consume("work", len(payload) + 1)
        self._admitted = True

    def decode(self, raw: bytes) -> object:
        """Decode one admitted receipt using only its fixed funded host controls."""
        if not self._admitted or self._decoded or self._closed:
            raise InputError("Terminal response is not available for decoding")
        self._decoded = True
        if type(raw) is not bytes:
            raise InputError("Terminal responses require immutable bytes")
        if len(raw) > self._cap:
            raise LimitExceeded("Reply exceeds funded terminal response capacity")
        self._host.consume("bytes", 8 * len(raw) + 1024)
        self._host.consume("work", len(raw) + 1)
        return decode_json(
            raw,
            max_bytes=self._cap,
            max_depth=self._host.limits.max_depth,
            budget=self._host,
        )

    def close(self) -> None:
        """Pay completed host controls once, including partial decode failure."""
        if not self._closed:
            self._closed = True
            self._host_costs.settle(self._host.consumed())
