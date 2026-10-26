"""Stable before/after finding identity and finite evidence classification."""

from collections import defaultdict
from typing import Literal

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget

from .types import ContractChange


def _key(issue: Issue) -> tuple[object, ...]:
    """Prose and changed diagnostic values do not invent a newly failing contract."""
    return issue.code, issue.location, issue.kind, issue.severity, issue.details


def _contract_changes(
    name: str,
    before: Report,
    after: Report,
    budget: Budget,
    changes: list[ContractChange],
) -> None:
    """Compare stable finding occurrences without erasing partial results."""
    previous: dict[tuple[object, ...], list[Issue]] = defaultdict(list)
    current: dict[tuple[object, ...], list[Issue]] = defaultdict(list)
    for issue in before.issues:
        budget.consume("work")
        previous[_key(issue)].append(issue)
    for issue in after.issues:
        budget.consume("work")
        current[_key(issue)].append(issue)
    for key in sorted(previous.keys() | current.keys(), key=repr):
        old, new = previous.get(key, []), current.get(key, [])
        for index in range(max(len(old), len(new))):
            budget.consume("work")
            left = old[index] if index < len(old) else None
            right = new[index] if index < len(new) else None
            status: Literal["introduced", "resolved", "persistent", "uncertain"] = (
                "persistent"
                if left is not None and right is not None
                else "introduced"
                if left is None and before.complete and before.checked
                else "resolved"
                if right is None and after.complete and after.checked
                else "uncertain"
            )
            budget.consume("states")
            changes.append(ContractChange(status, name, left, right))
