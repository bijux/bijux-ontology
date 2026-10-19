"""Explicit acceptance and verified serialization of checked RDF migrations."""

from bijux_ontology.documents.export import RDFExport, export_rdf
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate, _retain
from bijux_ontology.reports import Issue, Report


def export_migration(
    candidate: MigrationCandidate,
    *,
    accept: bool = False,
    format: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> RDFExport:
    """Export only an explicitly accepted candidate with complete selected checks.

    Existing RDF export verifies round-trip fidelity and required empty graphs;
    unsupported format capabilities remain unavailable without weakening scope.
    """
    if type(accept) is not bool or not isinstance(candidate, MigrationCandidate):
        raise InputError(
            "Migration export requires a typed candidate and boolean acceptance"
        )
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Migration export limits conflict with the operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    try:
        _retain(candidate.report, operation, issues)
        if not accept or candidate.source is None or not candidate.report.verified:
            issues.append(
                Issue(
                    "rdf.migration_acceptance_unverified",
                    "Export requires explicit acceptance and verified migration checks",
                    kind="unverified",
                )
            )
        else:
            exported = export_rdf(
                candidate.source,
                format=format,
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
            _retain(exported.report, operation, issues)
            return RDFExport(
                exported.content,
                Report(
                    "rdf.migration_export",
                    exported.report.checked,
                    tuple(issues),
                    requested=exported.report.requested,
                    children=(candidate.report, exported.report),
                    regime="accepted-rdf-migration-export",
                ),
                format,
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.migration_export")
    return RDFExport(
        None,
        Report(
            "rdf.migration_export", issues=tuple(issues), requested=1, complete=False
        ),
        format,
    )
