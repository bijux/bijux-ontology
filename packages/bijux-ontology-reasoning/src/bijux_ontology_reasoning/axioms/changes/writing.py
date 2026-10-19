"""Explicit acceptance and atomic publication of sealed native candidates."""

from pathlib import Path
from threading import Event

from bijux_ontology.documents.writing import write_bytes
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from .checking import _owned, receipt
from .types import AxiomCandidate


def write_axiom_change(
    candidate: AxiomCandidate,
    destination: Path,
    *,
    accept: bool = False,
    overwrite: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> Report:
    """Publish unchanged verified bytes through the existing atomic destination.

    This synchronous local filesystem publication is atomic, not a remote or
    multi-file transaction. Cancellation is checked before entering publication;
    failures leave the old destination intact and clean the private write file.
    """
    if (
        type(candidate) is not AxiomCandidate
        or type(accept) is not bool
        or type(overwrite) is not bool
        or not isinstance(destination, Path)
    ):
        raise InputError(
            "Publication needs an axiom candidate, path and boolean policies"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    published = False
    try:
        bound_text(str(destination), operation)
        retain_report(candidate.report, operation, issues)
        source = candidate.source
        if not accept or source is None or not candidate.report.verified:
            issues.append(
                Issue(
                    "owl.axiom_acceptance_unverified",
                    "Publication needs explicit acceptance and checked postconditions",
                    kind="unverified",
                )
            )
        elif (
            source is not candidate._checked_source
            or receipt(source) != candidate._receipt
        ):
            issues.append(
                Issue(
                    "owl.axiom_candidate_changed",
                    "Candidate bytes or metadata differ from the checked receipt",
                    kind="unverified",
                )
            )
        else:
            owned = _owned(source, operation)
            operation.consume("work")
            write_bytes(owned.content, destination, overwrite=overwrite)
            published = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.axiom_publication")
    return Report(
        "owl.axiom_publication",
        int(published),
        tuple(issues),
        requested=1,
        complete=published,
        regime="explicitly-accepted-atomic-native-file",
    )
