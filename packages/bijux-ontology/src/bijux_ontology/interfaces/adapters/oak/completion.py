"""Require actual complete OAK observations before any native result publication."""

from bijux_ontology.diagnostics.reports import Report

from .types import OakOperationError


def require_complete(report: Report) -> None:
    """Do not hide findings, missing coverage or an exhausted result prefix."""
    if not report.verified and not (
        report.outcome == "not_applicable"
        and report.complete
        and report.requested == 0
        and report.checked == 0
        and not report.issues
    ):
        raise OakOperationError(report)
