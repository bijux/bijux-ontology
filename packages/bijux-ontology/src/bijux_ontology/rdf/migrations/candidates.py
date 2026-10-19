"""Selected migration checks and explicit acceptance before verified RDF export."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import chain

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import capture_items, capture_source
from bijux_ontology.rdf.patches.types import DatasetPatch
from bijux_ontology.rdf.roles.classification import bound_text
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report


@dataclass(frozen=True)
class MigrationCheck:
    """One explicit contract callback with ordinary caller-owned Python state."""

    name: str
    check: Callable[[PreparedSource], Report]

    def __post_init__(self) -> None:
        """Require a bounded name and an actual callable without invoking it."""
        bound_text(self.name, Budget())
        if not self.name or not callable(self.check):
            raise InputError("Migration checks require a nonempty name and callable")


@dataclass(frozen=True)
class MigrationCandidate:
    """A reviewable copied candidate, exact edits and retained selected checks.

    Candidate availability does not mean application acceptance. Use the explicit
    export gate after inspecting the complete report and the proposed patch.
    """

    source: PreparedSource | None
    patch: DatasetPatch | None
    before: tuple[Report, ...]
    after: tuple[Report, ...]
    report: Report


def _retain(report: Report, operation: Budget, issues: IssueCollector) -> None:
    """Bound adopted report trees without counting inherited diagnostics twice."""
    pending = [(report, 0)]
    reports: set[int] = set()
    retained: set[int] = set()
    text: str | None
    while pending:
        current, depth = pending.pop()
        operation.consume("work")
        if depth > operation.limits.max_depth:
            raise LimitExceeded("Migration report exceeds max_depth")
        if id(current) in reports:
            continue
        operation.consume("states")
        reports.add(id(current))
        for number in (current.checked, current.requested):
            if number is not None:
                if number.bit_length() > min(4096, operation.limits.max_bytes):
                    raise LimitExceeded("Migration report counter exceeds max_bytes")
                bound_text(str(number), operation)
        operation.consume("states", len(current.families) + len(current.exclusions))
        for text in chain(
            (current.scope, current.regime), current.families, current.exclusions
        ):
            bound_text(text, operation)
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
                    bound_text(text, operation)
            if issue.details is not None:
                details = issue.details
                for text in (
                    details.source,
                    details.term,
                    details.graph,
                    details.model,
                ):
                    if text is not None:
                        bound_text(text, operation)
                operation.consume("states", len(details.path))
                for value in chain(details.path, (details.branch,)):
                    if value is None:
                        continue
                    if isinstance(value, int) and value.bit_length() > min(
                        4096, operation.limits.max_bytes
                    ):
                        raise LimitExceeded(
                            "Migration diagnostic index exceeds max_bytes"
                        )
                    bound_text(str(value), operation)
            issues.append(issue)
        operation.consume("states", len(current.children))
        pending.extend((child, depth + 1) for child in current.children)


def finish_migration(
    original: PreparedSource,
    transformed: Transformation,
    patch: DatasetPatch | None,
    *,
    before_checks: Sequence[MigrationCheck],
    after_checks: Sequence[MigrationCheck],
    budget: Operation,
) -> MigrationCandidate:
    """Run distinct named contracts on fresh authorized before/after snapshots.

    Callbacks are trusted in-process Python code. Their deliberate exceptions
    propagate; checks cannot mutate the original or another check's snapshot.
    A missing after-contract inventory leaves acceptance unverified. Baseline
    findings remain evidence and do not forbid explicitly adopting a valid repair.
    """
    issues = IssueCollector(budget)
    reports: tuple[list[Report], list[Report]] = ([], [])
    children: list[Report] = []
    completed = 0
    scoped_source = None
    try:
        _retain(transformed.report, budget, issues)
        children.append(transformed.report)
        completed = transformed.report.checked
        if patch is None or transformed.source is None:
            issues.append(
                Issue(
                    "rdf.migration_contracts_unverified",
                    "Migration requires a checked patch and available candidate",
                    kind="unverified",
                )
            )
        checks = (
            capture_items(before_checks, MigrationCheck, budget),
            capture_items(after_checks, MigrationCheck, budget),
        )
        if transformed.source is not None and transformed.report.verified:
            candidate_capture = capture_source(transformed.source, "candidate:", budget)
            scoped_source = _materialize(
                candidate_capture.witness, transformed.source, budget
            )
            for side, (role, source, selected) in enumerate(
                zip(
                    ("before", "after"),
                    (original, scoped_source),
                    checks,
                    strict=True,
                )
            ):
                seen: set[str] = set()
                selected_issues = issues if role == "after" else IssueCollector(budget)
                if not selected:
                    selected_issues.append(
                        Issue(
                            "rdf.migration_contracts_unverified",
                            "No selected migration contracts were supplied "
                            "for this side",
                            role,
                            kind="unverified",
                        )
                    )
                    if role == "before":
                        reports[side].append(
                            Report(
                                "migration.before",
                                issues=tuple(selected_issues),
                                requested=1,
                                complete=False,
                            )
                        )
                    continue
                assert source is not None
                captured = (
                    candidate_capture
                    if role == "after"
                    else capture_source(source, role + ":", budget)
                )
                for declaration in selected:
                    bound_text(declaration.name, budget)
                    if declaration.name in seen:
                        raise InputError(
                            "Migration check names must be distinct on each side"
                        )
                    budget.consume("states")
                    seen.add(declaration.name)
                    owned = _materialize(captured.witness, source, budget)
                    checked = declaration.check(owned)
                    budget.consume("work")
                    if not isinstance(checked, Report):
                        raise InputError(
                            "Migration contract callbacks must return Report"
                        )
                    _retain(checked, budget, selected_issues)
                    size = (
                        len(role.encode("utf-8"))
                        + 1
                        + len(declaration.name.encode("utf-8"))
                    )
                    if size > budget.limits.max_bytes:
                        raise LimitExceeded("Migration check name exceeds max_bytes")
                    budget.consume("bytes", size)
                    anchor = role + "." + declaration.name
                    checked = replace(checked, scope=anchor)
                    reports[side].append(checked)
                    if role == "after":
                        if not checked.checked:
                            issues.append(
                                Issue(
                                    "rdf.migration_contracts_unverified",
                                    "An after contract completed no actual comparison",
                                    anchor,
                                    kind="unverified",
                                )
                            )
                        children.append(checked)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.migration")
    report = Report(
        "rdf.migration",
        completed,
        tuple(issues),
        requested=max(1, completed),
        children=tuple(children),
        regime="selected-rdf-migration-contracts",
    )
    return MigrationCandidate(
        scoped_source, patch, tuple(reports[0]), tuple(reports[1]), report
    )
