"""Owned finite worker grants and exactly-once terminal counter settlement."""

from collections.abc import Mapping

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, BudgetKind


def _counts(value: object, maxima: Mapping[BudgetKind, int]) -> dict[BudgetKind, int]:
    """Validate every wire kind before returning any counter for adoption."""
    if not isinstance(value, dict) or set(value) != set(BUDGET_KINDS):
        raise InputError("Expected all delegated allowance counters")
    result: dict[BudgetKind, int] = {}
    for kind in BUDGET_KINDS:
        amount = value[kind]
        if type(amount) is not int or not 0 <= amount <= maxima[kind]:
            raise InputError("Delegated counter exceeds its finite allowance")
        result[kind] = amount
    return result


class DelegatedAllowance:
    """Snapshot one grant without changing a domain's reservation policy.

    A counter receipt settles at most once, even after cancellation. Invalid
    receipts cannot be retried. Domain/report qualification remains the caller's
    responsibility: after a valid settlement, accepted output keeps unused quota;
    rejected output may explicitly forfeit the still-unpaid grant remainder.
    Neither settlement nor forfeiture authorizes further worker execution.
    """

    def __init__(self, owner: Budget, grants: dict[BudgetKind, int]) -> None:
        """Capture all six maxima from the caller's currently available budget."""
        if not isinstance(owner, Budget):
            raise InputError("Delegated allowances require a Budget")
        self._owner = owner
        self._grants = _counts(
            grants, {kind: owner.remaining(kind) for kind in BUDGET_KINDS}
        )
        self._paid: dict[BudgetKind, int] = dict.fromkeys(BUDGET_KINDS, 0)
        self._attempted = False

    def to_data(self) -> dict[BudgetKind, int]:
        """Return an independent wire dictionary without exposing owned maxima."""
        return dict(self._grants)

    def _debit(self, kind: BudgetKind, amount: int) -> None:
        """Record actual paid capacity even if an owner rejects a later debit."""
        before = self._owner.remaining(kind)
        try:
            if isinstance(self._owner, Operation):
                self._owner.consume_completed(kind, amount)
            else:
                self._owner.consume(kind, amount)
        finally:
            paid = max(0, before - self._owner.remaining(kind))
            self._paid[kind] = min(self._grants[kind], self._paid[kind] + paid)

    def settle(self, counters: object) -> dict[BudgetKind, int]:
        """Validate the whole receipt, then adopt its costs once without live work."""
        if self._attempted:
            raise InputError("Delegated allowance already reached terminal settlement")
        self._attempted = True
        try:
            values = _counts(counters, self._grants)
        except InputError as error:
            raise ResourceError("Malformed delegated worker counters") from error
        for kind in BUDGET_KINDS:
            self._debit(kind, values[kind])
        return values

    def forfeit(self) -> None:
        """Idempotently consume only unpaid capacity without claiming native costs.

        Call only after an admitted worker lacks a qualified terminal result.
        Partial settlement is retained; repeated calls cannot double debit it.
        A concrete owner's stricter failure remains visible to the caller.
        """
        self._attempted = True
        for kind in BUDGET_KINDS:
            remaining = min(
                self._grants[kind] - self._paid[kind], self._owner.remaining(kind)
            )
            if remaining:
                self._debit(kind, remaining)
