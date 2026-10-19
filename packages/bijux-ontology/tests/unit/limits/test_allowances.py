"""Terminal settlement cannot refund or double debit a delegated worker grant."""

from threading import Event
from typing import Any

import pytest

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, BudgetKind, Limits


@pytest.mark.parametrize(
    "change",
    ["missing", "extra", "negative", "boolean", "fraction", "excessive", "late"],
)
def test_invalid_terminal_receipt_cannot_partly_pay_or_retry(change: str) -> None:
    owner = Budget()
    grants = dict.fromkeys(BUDGET_KINDS, 4)
    delegated = DelegatedAllowance(owner, grants)
    raw: dict[str, Any] = dict.fromkeys(BUDGET_KINDS, 1)
    if change == "missing":
        del raw["states"]
    elif change == "extra":
        raw["unknown"] = 1
    else:
        raw["states" if change == "late" else "bytes"] = {
            "negative": -1,
            "boolean": True,
            "fraction": 1.0,
            "excessive": 5,
            "late": True,
        }[change]
    with pytest.raises(InputError):
        delegated.settle(raw)
    assert owner._counts == {}
    with pytest.raises(InputError):
        delegated.settle(dict.fromkeys(BUDGET_KINDS, 1))
    delegated.forfeit()
    paid = dict(owner._counts)
    assert paid == grants
    delegated.forfeit()
    assert owner._counts == paid


def test_terminal_settlement_owns_grant_and_survives_nested_cancellation() -> None:
    owner = Budget()
    stop = Event()
    outer = Operation(None, owner, 30, stop)
    inner = Operation(None, outer, 30, None)
    grants = dict.fromkeys(BUDGET_KINDS, 4)
    delegated = DelegatedAllowance(inner, grants)
    grants["bytes"] = 0
    exported = delegated.to_data()
    exported["bytes"] = 0
    stop.set()
    assert delegated.settle(dict.fromkeys(BUDGET_KINDS, 1)) == dict.fromkeys(
        BUDGET_KINDS, 1
    )
    assert owner._counts == dict.fromkeys(BUDGET_KINDS, 1)
    with pytest.raises(InputError):
        delegated.settle(dict.fromkeys(BUDGET_KINDS, 1))
    # Domain adoption may reject a valid counter receipt; burn only its unpaid
    # remainder. Accepted output would simply keep the unused capacity.
    delegated.forfeit()
    delegated.forfeit()
    assert owner._counts == dict.fromkeys(BUDGET_KINDS, 4)


def test_partial_concrete_owner_failure_cannot_double_charge_completed_kinds() -> None:
    owner = Budget(Limits(max_total_bytes=16, max_states=4))
    grants = dict.fromkeys(BUDGET_KINDS, 4)
    delegated = DelegatedAllowance(owner, grants)
    owner.consume("states", 4)
    with pytest.raises(LimitExceeded):
        delegated.settle(dict.fromkeys(BUDGET_KINDS, 1))
    delegated.forfeit()
    before = dict(owner._counts)
    delegated.forfeit()
    assert owner._counts == before == dict.fromkeys(BUDGET_KINDS, 4)
    assert owner.remaining("bytes") == 12


def test_owner_debit_then_failure_retains_actual_paid_capacity() -> None:
    class StrictBudget(Budget):
        """A concrete owner can reject after recording one actual debit."""

        failed = False

        def consume(self, kind: BudgetKind, amount: int = 1) -> None:
            super().consume(kind, amount)
            if kind == "resources" and not self.failed:
                self.failed = True
                raise LimitExceeded("Owner stopped after its resource debit")

    owner = StrictBudget()
    delegated = DelegatedAllowance(owner, dict.fromkeys(BUDGET_KINDS, 4))
    with pytest.raises(LimitExceeded):
        delegated.settle(dict.fromkeys(BUDGET_KINDS, 1))
    delegated.forfeit()
    delegated.forfeit()
    assert owner._counts == dict.fromkeys(BUDGET_KINDS, 4)
