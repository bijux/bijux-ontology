"""Faithful portable RDF output for separately owned shape and ontology roles."""

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.export import RDFExport, export_rdf
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)

from .types import ProfileExport, ShapeProfile


def export_profile(
    profile: ShapeProfile,
    *,
    format: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> ProfileExport:
    """Export qualified roles with maintained serialization/reparse verification.

    Structural artifacts remain directly inspectable through profile.source even
    when optional validation or supplied-case coverage is unavailable. This gate
    does not label such an artifact a behavior-qualified portable profile.
    """
    if not isinstance(profile, ShapeProfile):
        raise InputError("Profile export requires a typed ShapeProfile")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Profile export limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    shapes: RDFExport | None = None
    ontology: RDFExport | None = None
    children: list[Report] = []
    try:
        retain_report(profile.report, operation, issues)
        children.append(profile.report)
        if profile.source is None or not profile.report.verified:
            issues.append(
                Issue(
                    "shacl.profile_behavior_unverified",
                    "Portable profile qualification requires "
                    "completed supplied-case agreement",
                    kind="unverified",
                )
            )
        else:
            shapes = export_rdf(
                profile.source,
                format=format,
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
            retain_report(shapes.report, operation, issues)
            children.append(shapes.report)
            if profile.ontology is not None:
                ontology = export_rdf(
                    profile.ontology,
                    format=format,
                    budget=operation,
                    timeout_seconds=timeout_seconds,
                )
                retain_report(ontology.report, operation, issues)
                children.append(ontology.report)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "shacl-profile-export",
        int(shapes is not None and shapes.content is not None),
        tuple(issues),
        requested=1,
        children=tuple(children),
        regime="faithful-role-separated-profile-export",
    )
    return ProfileExport(shapes, ontology, report)
