"""Selected migration checks and explicit acceptance before verified RDF export."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, retain_report
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
        retain_report(transformed.report, budget, issues)
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
                    retain_report(checked, budget, selected_issues)
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
