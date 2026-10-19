"""Per-operation budgets; no global counters or post-hoc timeout claims."""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from itertools import chain
from typing import Literal

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.reports import Issue, Report

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
        self._retained_issues: dict[int, Issue] = {}

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

    def remaining(self, kind: BudgetKind) -> int:
        """Expose the cumulative allowance available to a bounded owned worker."""
        name = "max_total_bytes" if kind == "bytes" else f"max_{kind}"
        if not hasattr(self.limits, name):
            raise InputError(f"Unknown budget: {kind}")
        return int(getattr(self.limits, name)) - self._counts.get(kind, 0)

    def _retain_issue(self, issue: Issue, *, repeated: bool) -> None:
        """Transfer paid diagnostics; repeated local retention still costs a slot."""
        identity = id(issue)
        if repeated or identity not in self._retained_issues:
            self.consume("issues")
            self._retained_issues.setdefault(identity, issue)


class IssueCollector(list[Issue]):
    """Bound retained ordinary findings, reserving one final exhaustion marker."""

    def __init__(self, budget: Budget) -> None:
        super().__init__()
        self.budget = budget
        self._retained_ids: set[int] = set()

    def append(self, issue: Issue) -> None:
        """Retain a diagnostic only when its operation allowance remains."""
        identity = id(issue)
        self.budget._retain_issue(issue, repeated=identity in self._retained_ids)
        self._retained_ids.add(identity)
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


def _report_text(value: str, budget: Budget) -> None:
    """Bound inherited report text before allocating its UTF-8 representation."""
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Adopted report metadata exceeds max_bytes")
    budget.consume("work", len(value) + 1)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Adopted report metadata requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Adopted report metadata exceeds max_bytes")
    budget.consume("bytes", size)


def retain_report(report: Report, operation: Budget, issues: IssueCollector) -> None:
    """Bound adopted report trees without counting inherited diagnostics twice."""
    pending = [(report, 0)]
    reports: set[int] = set()
    retained: set[int] = set()
    text: str | None
    while pending:
        current, depth = pending.pop()
        operation.consume("work")
        if depth > operation.limits.max_depth:
            raise LimitExceeded("Adopted report exceeds max_depth")
        if id(current) in reports:
            continue
        operation.consume("states")
        reports.add(id(current))
        for number in (current.checked, current.requested):
            if number is not None:
                if number.bit_length() > min(4096, operation.limits.max_bytes):
                    raise LimitExceeded("Adopted report counter exceeds max_bytes")
                _report_text(str(number), operation)
        operation.consume("states", len(current.families) + len(current.exclusions))
        for text in chain(
            (current.scope, current.regime), current.families, current.exclusions
        ):
            _report_text(text, operation)
        for issue in current.issues:
            operation.consume("work")
            if id(issue) in retained:
                continue
            operation.consume("states")
            retained.add(id(issue))
            for text in (
                issue.code,
                issue.message,
                issue.location,
                issue.expected,
                issue.actual,
                issue.evidence_json,
            ):
                if text is not None:
                    _report_text(text, operation)
            if issue.details is not None:
                details = issue.details
                for text in (
                    details.source,
                    details.term,
                    details.graph,
                    details.model,
                ):
                    if text is not None:
                        _report_text(text, operation)
                operation.consume("states", len(details.path))
                for value in chain(details.path, (details.branch,)):
                    if value is None:
                        continue
                    if isinstance(value, int) and value.bit_length() > min(
                        4096, operation.limits.max_bytes
                    ):
                        raise LimitExceeded(
                            "Adopted diagnostic index exceeds max_bytes"
                        )
                    _report_text(str(value), operation)
                if details.failure is not None:
                    failure = details.failure
                    operation.consume("states", len(failure.import_chain) + 1)
                    for text in chain(
                        (failure.document_iri or "", failure.location, failure.role),
                        failure.import_chain,
                    ):
                        _report_text(text, operation)
                    if failure.span is not None:
                        for value in failure.span.to_dict().values():
                            if value is not None:
                                if value.bit_length() > min(
                                    4096, operation.limits.max_bytes
                                ):
                                    raise LimitExceeded(
                                        "Parser coordinate exceeds max_bytes"
                                    )
                                _report_text(str(value), operation)
            issues.append(issue)
        operation.consume("states", len(current.children))
        pending.extend((child, depth + 1) for child in current.children)
