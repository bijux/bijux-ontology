"""Explicit acceptance and verified serialization of checked RDF migrations."""

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.export import RDFExport, export_rdf
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata


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
        retain_report(candidate.report, operation, issues)
        if not accept or candidate.source is None or not candidate.report.verified:
            issues.append(
                Issue(
                    "rdf.migration_acceptance_unverified",
                    "Export requires explicit acceptance and verified migration checks",
                    kind="unverified",
                )
            )
        elif (
            candidate.source is not candidate._checked_source
            or candidate._metadata is None
            or not candidate._metadata.same_scope(
                SnapshotMetadata.capture(candidate.source, operation)
            )
        ):
            issues.append(
                Issue(
                    "rdf.migration_acceptance_unverified",
                    "Candidate scope, origin or fidelity differs "
                    "from its checked receipt",
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
            retain_report(exported.report, operation, issues)
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
