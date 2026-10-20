"""Explicit code comparison without conflating source completeness and identity."""

from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri

from ..identity import FhirCodeIdentity
from .reading import _operation
from .types import FhirCodeMembership, FhirCodeSystem, FhirConcept


def check_code_membership(
    system: FhirCodeSystem,
    identity: FhirCodeIdentity,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FhirCodeMembership:
    """Compare only the exact selected system/version under declared case policy.

    ASCII comparison is an explicit application regime for a case-insensitive
    source. No Unicode case fold, inferred business version or changed source
    spelling is used. Open content cannot prove that an absent code is invalid.
    """
    if not isinstance(system, FhirCodeSystem) or type(identity) is not FhirCodeIdentity:
        raise InputError("Membership requires an owned CodeSystem and exact identity")
    operation = _operation(limits, budget)
    issues = IssueCollector(operation)
    matched: FhirConcept | None = None
    status: Literal["found", "absent", "unknown"] = "unknown"
    checked = 0
    source_reports: tuple[Report, ...] = ()
    try:
        for value in (identity.system, identity.code):
            if type(value) is not str or not value:
                raise InputError("FHIR membership requires original nonempty text")
            bound_text(value, operation)
        if identity.version is not None:
            if type(identity.version) is not str or not identity.version:
                raise InputError("FHIR membership version requires original text")
            bound_text(identity.version, operation)
        absolute_iri(identity.system)
        retain_report(system.report, operation, issues)
        source_reports = (system.report,)
        if not system.report.verified:
            raise _Unknown("The supplied CodeSystem interpretation is unqualified")
        bound_text(system.system, operation)
        if system.version is not None:
            bound_text(system.version, operation)
        if identity.system != system.system or identity.version != system.version:
            raise _Unknown("The exact requested system/version is not supplied")
        if system.content == "supplement":
            raise _Unknown("A supplement does not establish standalone code membership")
        candidates = []
        for concept in system.concepts:
            operation.consume("work")
            operation.consume("states")
            bound_text(concept.identity.code, operation)
            operation.consume("work", len(identity.code) + len(concept.identity.code))
            if system.case_sensitive is False:
                if system.case_comparison != "ascii":
                    raise _Unknown(
                        "Case-insensitive membership requires an explicit "
                        "comparison regime"
                    )
                if not identity.code.isascii() or not concept.identity.code.isascii():
                    raise _Unknown(
                        "The selected ASCII regime does not qualify Unicode comparison"
                    )
                operation.consume(
                    "work", len(identity.code) + len(concept.identity.code)
                )
                operation.consume(
                    "bytes", len(identity.code) + len(concept.identity.code)
                )
                same = identity.code.lower() == concept.identity.code.lower()
            else:
                same = identity.code == concept.identity.code
            if same:
                candidates.append(concept)
        if len(candidates) > 1:
            raise _Unknown("Declared comparison selects multiple distinct source codes")
        if candidates:
            matched, status = candidates[0], "found"
        elif system.case_sensitive is None:
            raise _Unknown("An absent case policy cannot qualify lexical absence")
        elif system.content != "complete":
            raise _Unknown("Open CodeSystem content cannot establish code absence")
        else:
            status = "absent"
        checked = 1
    except _Unknown as error:
        try:
            issues.append(
                Issue(
                    "adapter.fhir_membership_unverified", str(error), kind="unverified"
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining membership qualification")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "adapter.fhir_code_membership",
        checked,
        tuple(issues),
        requested=1,
        complete=not any(item.kind in {"unverified", "exhausted"} for item in issues),
        children=source_reports,
        regime="fhir-r5-declared-case",
    )
    if not report.verified:
        matched, status = None, "unknown"
    return FhirCodeMembership(
        identity,
        matched.identity if matched is not None else None,
        matched,
        status,
        report,
    )


class _Unknown(Exception):
    """Preserve incomplete semantic evidence without inventing negative membership."""
