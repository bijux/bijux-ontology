"""Cumulative cancellation, scalar text and filter admission for vocabulary reads."""

from collections.abc import Sequence
from threading import Event

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, BudgetKind
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag


class _PageBudget(Budget):
    """Forward one allowance while checking cancellation during lazy preparation."""

    def __init__(self, budget: Budget, cancel: Event | None) -> None:
        """Borrow counters; never reset or inherit a separate operation budget."""
        self.limits = budget.limits
        self._budget = budget
        self._cancel = cancel

    def remaining(self, kind: BudgetKind) -> int:
        """Expose the same cumulative allowance used by forwarded consumption."""
        return self._budget.remaining(kind)

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Check cancellation before each preparation or candidate work unit."""
        if self._cancel is not None and self._cancel.is_set():
            raise LimitExceeded("Vocabulary browsing cancelled")
        self._budget.consume(kind, amount)


def _text(value: str, budget: Budget) -> None:
    """Bound scalar UTF-8 text before capture, validation or normalization."""
    if not isinstance(value, str):
        raise InputError("Page query and filter text must be strings")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Page text exceeds max_bytes")
    budget.consume("work", len(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Page text requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Page text exceeds max_bytes")
    budget.consume("bytes", size)


def _comparison_text(value: str, budget: Budget, *, fold: bool = False) -> str:
    """Charge text conversion and optional Unicode casefold before allocating."""
    _text(value, budget)
    budget.consume("work", len(value))
    budget.consume("bytes", 64 + 4 * len(value))
    result = str(value)
    if fold:
        # Unicode full casefold expands each input character to at most three.
        budget.consume("work", 3 * len(value))
        budget.consume("bytes", 64 + 12 * len(value))
        result = result.casefold()
    return result


def _strings(
    values: Sequence[str],
    budget: Budget,
    *,
    languages: bool = False,
    iri_field: bool = False,
) -> tuple[str, ...]:
    """Own finite filter sets; equivalent filter order has equivalent meaning."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Page filters require a finite sequence of strings")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count if count else 0)
    selected: list[str] = []
    for index in range(count):
        budget.consume("work")
        value = values[index]
        _text(value, budget)
        if languages:
            language_tag(value)
            budget.consume("work", len(value))
            budget.consume("bytes", 64 + 4 * len(value))
            value = value.lower()
        elif not (iri_field and value == "iri"):
            absolute_iri(value)
        selected.append(value)
    budget.consume("work", count * max(1, count.bit_length()))
    return tuple(sorted(set(selected)))


def _filter_set(values: tuple[str, ...], budget: Budget) -> frozenset[str]:
    """Prepare exact membership once, charging retained references and hashing."""
    budget.consume("work", sum(len(value) + 1 for value in values))
    budget.consume("bytes", 128 + 64 * len(values) if values else 0)
    return frozenset(values)
