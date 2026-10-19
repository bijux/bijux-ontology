"""Explicit final-plan acceptance through the ordinary checked RDF exporter."""

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.export import RDFExport, export_rdf
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.correspondence import compare_witnesses
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import PatchUnavailable

from .execution import _remaining
from .types import MigrationPlanResult


def export_migration_plan(
    result: MigrationPlanResult,
    *,
    accept: bool = False,
    format: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> RDFExport:
    """Serialize only the checked final snapshot after explicit caller acceptance.

    The returned bytes retain ordinary roundtrip qualification. Use write_export
    with an explicitly selected destination; execution itself performs no writes.
    Ordered native patches remain in each operation result, not a guessed combined
    patch spanning different blank identity spaces.
    """
    if not isinstance(result, MigrationPlanResult) or type(accept) is not bool:
        raise InputError("Plan export requires a typed result and boolean acceptance")
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Plan export limits conflict with the operation budget")
    operation = Operation(limits, budget, timeout_seconds, None)
    issues = IssueCollector(operation)
    children = [result.report]
    content = None
    checked = 0
    try:
        retain_report(result.report, operation, issues)
        if (
            not accept
            or result.source is None
            or result._witness is None
            or not result.report.verified
        ):
            issues.append(
                Issue(
                    "upgrade.plan_unverified",
                    "Export requires acceptance and a verified final migration",
                    kind="unverified",
                )
            )
        else:
            actual = capture_source(result.source, "export:", operation)
            agrees, _ = compare_witnesses(result._witness, actual.witness, operation)
            checked = 1
            if not agrees:
                issues.append(
                    Issue(
                        "upgrade.plan_final_changed",
                        "The final candidate differs from its checked snapshot",
                    )
                )
            else:
                owned = _materialize(actual.witness, result.source, operation)
                exported = export_rdf(
                    owned,
                    format=format,
                    budget=operation,
                    timeout_seconds=_remaining(operation),
                )
                retain_report(exported.report, operation, issues)
                children.append(exported.report)
                content = exported.content
                checked = exported.report.checked
    except PatchUnavailable as error:
        try:
            issues.append(
                Issue("upgrade.plan_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "migration.plan_export",
        checked,
        tuple(issues),
        requested=max(1, checked),
        children=tuple(children),
        regime="accepted-final-migration-export",
    )
    return RDFExport(content if report.verified else None, report, format)
