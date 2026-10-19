"""Actual logical observations and qualification of selected theory obligations."""

from dataclasses import replace

from rdflib import URIRef

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import IssueCollector, retain_report

from ..axioms.types import ProjectedAxiom
from ..backend.types import OwlProfileBackend
from ..inference.consistency.checking import check_consistency
from ..inference.entailment.checking import check_entailments
from .sources import reserve
from .types import TheoryComponent, TheoryObservation


def observe_theory(
    backend: OwlProfileBackend,
    theory: TheoryComponent,
    classes: tuple[URIRef, ...] | None,
    obligations: tuple[ProjectedAxiom, ...],
    operation: Operation,
    heap_mb: int,
    retained: list[TheoryObservation],
) -> TheoryObservation:
    """Record native truth before later contract execution or report adoption can stop.

    All-class inventory enables truthful absent-versus-unknown comparisons across
    components. Explicit empty class selection requests consistency only. Known
    negative consumer answers are completed observations, not a fabricated joint
    consumer failure; joint acceptance separately requires its actual answers.
    """
    reserve(operation, 8 + len(obligations) + len(theory.imports or {}))
    diagnosis = check_consistency(
        backend,
        theory.source,
        named_classes=() if classes == () else None,
        imports=theory.imports,
        budget=operation,
        heap_mb=heap_mb,
    )
    signature_complete = (
        diagnosis.consistent is True and classes != () and diagnosis.report.complete
    )
    observed = TheoryObservation(
        theory.label,
        theory.source,
        tuple((theory.imports or {}).items()),
        diagnosis,
        None,
        classes,
        obligations,
        signature_complete,
        Report(
            "owl.theory_observation",
            issues=(
                Issue(
                    "execution.incomplete",
                    "Theory observation is incomplete",
                    kind="unverified",
                ),
            ),
            requested=1,
            complete=False,
        ),
    )
    index = len(retained)
    retained.append(observed)
    entailments = None
    if obligations:
        entailments = check_entailments(
            backend,
            theory.source,
            obligations,
            imports=theory.imports,
            budget=operation,
            heap_mb=heap_mb,
        )
        observed = replace(observed, entailments=entailments)
        retained[index] = observed
    complete = diagnosis.consistent is not None and diagnosis.report.complete
    if obligations:
        complete = complete and entailments is not None and entailments.report.complete
    issues = IssueCollector(operation)
    if not complete:
        retain_report(diagnosis.report, operation, issues)
        if entailments is not None:
            retain_report(entailments.report, operation, issues)
    observed = replace(
        observed,
        report=Report(
            "owl.theory_observation",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime="native-outcome-and-signature-coverage",
        ),
    )
    retained[index] = observed
    return observed


def logical_report(observation: TheoryObservation, operation: Operation) -> Report:
    """Require actual joint truth only within the selected class/consumer scope."""
    reserve(operation, len(observation.consistency.classes) + 4)
    issues = IssueCollector(operation)
    diagnosis = observation.consistency
    reports = [
        diagnosis.consistency_report
        if diagnosis.consistent is not None
        else diagnosis.report
    ]
    selected = observation.named_classes
    present = set()
    for answer in diagnosis.classes:
        operation.consume("work")
        if selected is None or answer.iri in selected:
            reports.append(answer.report)
            present.add(answer.iri)
    if selected is not None:
        for iri in selected:
            if iri not in present:
                issues.append(
                    Issue(
                        "owl.integration_class_unverified",
                        "A selected joint class has no qualified native answer",
                        str(iri),
                        kind="unverified",
                    )
                )
    if observation.consumer_entailments:
        if observation.entailments is None:
            issues.append(
                Issue(
                    "owl.integration_contract_unverified",
                    "Selected joint consumer entailments were not checked",
                    observation.label,
                    kind="unverified",
                )
            )
        else:
            reports.append(observation.entailments.report)
    return Report(
        "owl.joint_compatibility",
        len(reports),
        tuple(issues),
        children=tuple(reports),
        regime="selected-joint-dl-consistency-satisfiability-consumers",
    )
