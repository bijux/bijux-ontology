"""Cumulative operation deadlines and worker allowance ownership."""

import sys
import threading
import time

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, BudgetKind, Limits
from bijux_ontology.execution.processes.transport import checkpoint


def operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Keep one caller's limits and counters throughout each owned operation."""
    if budget is not None and (
        not isinstance(budget, Budget) or limits is not None and limits != budget.limits
    ):
        raise InputError("Use the shared Budget with matching limits")
    return budget if budget is not None else Budget(limits)


class Operation(Budget):
    """Share host counters while qualifying every stage with one absolute deadline."""

    def __init__(
        self,
        limits: Limits | None,
        budget: Budget | None,
        timeout: float,
        cancel: threading.Event | None,
    ) -> None:
        """Validate execution ownership before reading any caller inventory."""
        if (
            limits is not None
            and not isinstance(limits, Limits)
            or budget is not None
            and not isinstance(budget, Budget)
        ):
            raise InputError("Operations require Limits and Budget objects")
        if limits is not None and budget is not None and limits != budget.limits:
            raise InputError("Operation limits conflict with the shared budget")
        if type(timeout) not in (float, int) or not 0 < timeout <= sys.float_info.max:
            raise InputError("Operation timeout must be finite and positive")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise InputError("Operation cancellation requires threading.Event")
        shared = budget if budget is not None else Budget(limits)
        if (
            isinstance(shared, Operation)
            and shared.cancel is not None
            and cancel is not None
            and cancel is not shared.cancel
        ):
            raise InputError("Nested operation cancellation requires the same Event")
        super().__init__(shared.limits)
        self.shared = shared
        self.cancel: threading.Event | None = cancel
        self._counts = shared._counts
        self._retained_issues = shared._retained_issues
        self.deadline = time.monotonic() + timeout
        if isinstance(shared, Operation):
            self.deadline = min(self.deadline, shared.deadline)
            if cancel is None:
                self.cancel = shared.cancel

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Charge the original operation counters without restarting its allowance."""
        checkpoint(self.deadline, self.cancel)
        self.shared.consume(kind, amount)

    def reserve_storage(self, amount: int) -> None:
        """Preserve shared allocation ownership through nested operation views."""
        checkpoint(self.deadline, self.cancel)
        self.shared.reserve_storage(amount)

    def remaining(self, kind: BudgetKind) -> int:
        """Read the actual shared allowance, including any enclosing operation."""
        return self.shared.remaining(kind)


class ControlBudget(Budget):
    """Bound worker-owned preparation to the caller's remaining allowances.

    Maintained engine internals remain bounded by their owned process deadline.
    These counters cover explicit decoding, preparation and protocol operations.
    """

    def __init__(self, limits: Limits, allowances: object) -> None:
        """Accept only complete finite nonnegative per-kind allowances."""
        super().__init__(limits)
        if not isinstance(allowances, dict) or set(allowances) != set(BUDGET_KINDS):
            raise InputError("Malformed operation worker allowances")
        self.allowances: dict[BudgetKind, int] = {}
        for kind in BUDGET_KINDS:
            amount = allowances[kind]
            if type(amount) is not int or not 0 <= amount <= super().remaining(kind):
                raise InputError("Invalid operation worker allowance")
            self.allowances[kind] = amount

    def remaining(self, kind: BudgetKind) -> int:
        """Subtract completed worker work from this request's initial allowance."""
        capacity = (
            self.limits.max_total_bytes
            if kind == "bytes"
            else getattr(self.limits, f"max_{kind}")
        )
        return self.allowances[kind] - (capacity - super().remaining(kind))

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Refuse work before it exceeds the caller's available cumulative bound."""
        if type(amount) is not int or amount < 0:
            raise InputError("Budget consumption must be a nonnegative integer")
        if amount > self.remaining(kind):
            raise LimitExceeded("Operation worker exhausted its shared allowance")
        super().consume(kind, amount)

    def consumed(self) -> dict[BudgetKind, int]:
        """Return exact completed owned work for validated host adoption."""
        return {
            kind: self.allowances[kind] - self.remaining(kind) for kind in BUDGET_KINDS
        }
