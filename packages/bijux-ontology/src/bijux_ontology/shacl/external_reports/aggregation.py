"""Required aggregation retaining each external or local SHACL report scope."""

from collections.abc import Sequence

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.shacl.evidence.results import ShaclResult

from .types import ExternalShaclReport, ShaclReportAggregate


def aggregate_shacl_reports(
    reports: Sequence[ExternalShaclReport | ShaclResult],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShaclReportAggregate:
    """Combine typed required results without merging their RDF blank identities.

    Tuple positions identify distinct report scopes. External assertions remain
    unverified even when combined with a current verified local operation.
    """
    shared = operation(limits, budget)
    issues = IssueCollector(shared)
    selected = []
    children = []
    complete = False
    try:
        values = capture(reports, shared)
        for current in values:
            if not isinstance(current, ExternalShaclReport | ShaclResult):
                raise InputError("Expected typed external or local SHACL results")
            shared.consume("states", 2)
            returned = current.report
            if not isinstance(returned, Report):
                raise InputError("SHACL results require a typed Report")
            retain_report(returned, shared, issues)
            selected.append(current)
            children.append(returned)
        if not selected:
            issues.append(
                Issue(
                    "shacl.external_aggregate_empty",
                    "No required SHACL report scopes were supplied",
                    kind="unverified",
                )
            )
        complete = bool(selected)
    except LimitExceeded:
        issues.exhausted("SHACL report aggregation exhausted its shared allowance")
    report = Report(
        "shacl-report-aggregate",
        sum(child.checked for child in children),
        tuple(issues),
        requested=max(1, sum(child.requested or 0 for child in children)),
        complete=complete,
        children=tuple(children),
        regime="separate-shacl-report-scopes",
    )
    result = object.__new__(ShaclReportAggregate)
    object.__setattr__(result, "results", tuple(selected))
    object.__setattr__(result, "report", report)
    return result
