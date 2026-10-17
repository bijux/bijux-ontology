"""Per-operation budgets; no global counters or post-hoc timeout claims."""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Literal

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.reports import Issue

BudgetKind = Literal["bytes", "resources", "triples", "work", "issues", "states"]


@dataclass(frozen=True)
class Limits:
    """Maximum owned input sizes and work, independently selectable by callers."""

    max_bytes: int = 5_000_000
    max_total_bytes: int = 50_000_000
    max_depth: int = 128
    max_resources: int = 128
    max_triples: int = 250_000
    max_work: int = 1_000_000
    max_issues: int = 10_000
    max_states: int = 100_000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise InputError("Expected a positive integer", location=field.name)


class Budget:
    """Count work within one operation and raise before exceeding its bound."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits if limits is not None else Limits()
        self._counts: dict[BudgetKind, int] = {}

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Consume a named allowance, retaining no cross-operation state."""
        if type(amount) is not int or amount < 0:
            raise InputError("Budget consumption must be a nonnegative integer")
        name = "max_total_bytes" if kind == "bytes" else f"max_{kind}"
        if not hasattr(self.limits, name):
            raise InputError(f"Unknown budget: {kind}")
        count = self._counts.get(kind, 0) + amount
        if count > getattr(self.limits, name):
            raise LimitExceeded(f"Exceeded {name}")
        self._counts[kind] = count


class IssueCollector(list[Issue]):
    """Bound retained ordinary findings, reserving one final exhaustion marker."""

    def __init__(self, budget: Budget) -> None:
        super().__init__()
        self.budget = budget

    def append(self, issue: Issue) -> None:
        """Retain a diagnostic only when its operation allowance remains."""
        self.budget.consume("issues")
        super().append(issue)

    def extend(self, issues: Iterable[Issue]) -> None:
        """Retain each completed finding before a later exhaustion occurs."""
        for issue in issues:
            self.append(issue)

    def exhausted(self, message: str, *, location: str = "") -> None:
        """Explain stopped checking without discarding completed findings."""
        if not any(issue.kind == "exhausted" for issue in self):
            super().append(
                Issue("execution.limit_exceeded", message, location, kind="exhausted")
            )
