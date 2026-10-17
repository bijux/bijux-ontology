"""Concise human display of complete reports; presentation never alters policy."""

from bijux_ontology.errors import InputError
from bijux_ontology.reports import AcceptancePolicy, Report


def format_report(
    report: Report, *, limit: int | None = None, policy: AcceptancePolicy | None = None
) -> str:
    """Include corrective values and disclose any undisplayed diagnostics."""
    if limit is not None and (type(limit) is not int or limit < 0):
        raise InputError("A display limit must be a nonnegative integer")
    rows = [
        f"{report.scope}: {report.checked}/{report.requested} comparisons; "
        f"outcome={report.outcome}; complete={report.complete}; "
        f"verified={report.verified}"
    ]
    if policy is not None:
        permitted = sum(policy.permits(issue) for issue in report.issues)
        rows.append(
            f"policy: accepted={report.exit_code(policy=policy) == 0}; "
            f"minimum_severity={policy.minimum_severity}; strict={policy.strict}; "
            f"permitted_diagnostics={permitted} "
            "(retained; permission never establishes verification)"
        )
    issues = report.issues if limit is None else report.issues[:limit]
    for issue in issues:
        rows.append(f"{issue.kind}: {issue.code}: {issue.location}: {issue.message}")
        if issue.expected is not None:
            rows.append(f"  expected: {issue.expected}")
        if issue.actual is not None:
            rows.append(f"  actual: {issue.actual}")
    hidden = len(report.issues) - len(issues)
    if hidden:
        rows.append(
            f"{hidden} additional diagnostics; use JSON output for the complete report."
        )
    return "\n".join(rows)
