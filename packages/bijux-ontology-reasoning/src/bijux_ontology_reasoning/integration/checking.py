"""Selected deployment checks with independent source and unbridged baselines."""

from collections.abc import Sequence
from threading import Event
from typing import Literal

from rdflib import URIRef

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.reports import Issue, Report

from ..axioms.codec.encoding import ConstructionEncoder
from ..axioms.types import ProjectedAxiom
from ..backend.types import OwlProfileBackend
from ..diagnosis.checking import _classes
from ..entailment.encoding import encode_axiom
from ..explanations.checking import explain_reasoning
from ..explanations.types import ExplanationCheck, ExplanationQuestion
from .comparison import compare_theories
from .preparation import (
    assemble_theory,
    capture_theories,
    derived_label,
    observe_theory,
    reserve,
)
from .types import (
    IntegrationCheck,
    JointCompatibility,
    TheoryCombination,
    TheoryComparison,
    TheoryComponent,
    TheoryInteraction,
    TheoryObservation,
)


def _logical_report(observation: TheoryObservation, operation: Operation) -> Report:
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


def _explain_interactions(
    backend: OwlProfileBackend,
    theory: TheoryComponent,
    comparison: TheoryComparison,
    stage: Literal["unbridged", "bridged"],
    operation: Operation,
    heap_mb: int,
    max_explanations: int,
    max_axioms: int,
    proofs: list[ExplanationCheck],
    interactions: list[TheoryInteraction],
    reports: list[Report],
    issues: IssueCollector,
) -> None:
    """Retain complete source witnesses without assigning unique causal ownership."""
    reserve(operation, len(comparison.newly_unsatisfiable) + 2)
    questions = (
        (ExplanationQuestion("inconsistency"),)
        if comparison.newly_inconsistent is True
        else tuple(
            ExplanationQuestion("unsatisfiability", class_iri=str(iri))
            for iri in comparison.newly_unsatisfiable
        )
    )
    for question in questions:
        if len(proofs) >= max_explanations:
            raise LimitExceeded("Selected integration explanations exceed their bound")
        reserve(operation, 8)
        proof = explain_reasoning(
            backend,
            theory.source,
            question,
            imports=theory.imports,
            max_axioms=max_axioms,
            budget=operation,
            heap_mb=heap_mb,
        )
        proofs.append(proof)
        reports.append(proof.explanation_report)
        if (
            proof.explanation_report.verified
            and proof.justification is not None
            and proof.justification.reproduced
        ):
            reserve(
                operation,
                1 + sum(len(axiom.origins) for axiom in proof.justification.axioms),
            )
            interactions.append(
                TheoryInteraction(
                    stage,
                    URIRef(question.class_iri)
                    if question.class_iri is not None
                    else None,
                    proof,
                    tuple(
                        origin
                        for axiom in proof.justification.axioms
                        for origin in axiom.origins
                    ),
                )
            )
        retain_report(proof.explanation_report, operation, issues)


def check_integration(
    backend: OwlProfileBackend,
    components: Sequence[TheoryComponent],
    combinations: Sequence[TheoryCombination],
    *,
    named_classes: Sequence[URIRef] | None = None,
    consumer_entailments: Sequence[ProjectedAxiom] = (),
    max_combinations: int = 8,
    max_explanations: int = 8,
    max_axioms: int = 64,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> IntegrationCheck:
    """Check explicit deployments against original and unbridged native baselines.

    The native class inventory is complete before absence can inform a change.
    Explicit class selection limits compared obligations; native preparation
    inventories the component signatures within the same bounds. Components,
    supplied native bridge documents and imports retain separate exact owners.
    Mapping records are never interpreted as OWL by this operation.

    Completion covers only selected combinations and bounded explanations. It
    never establishes compatibility of unselected subsets, all possible bridges
    or every semantic consequence. Unknown baselines prevent new-blame claims.
    Raw original consumer nonentailment remains separate from joint acceptance.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    observations: list[TheoryObservation] = []
    proofs: list[ExplanationCheck] = []
    results: list[JointCompatibility] = []
    reports: list[Report] = []
    requested = 1
    finished = False
    try:
        for value in (max_combinations, max_explanations, max_axioms):
            if type(value) is not int or value <= 0:
                raise InputError("Joint theory bounds require positive integers")
        classes = _classes(named_classes, operation)
        queries, _ = ConstructionEncoder(operation).axioms(consumer_entailments)
        for query in queries:
            encode_axiom(query, operation)
        catalog, selected = capture_theories(components, combinations, operation)
        requested = len(selected)
        baselines: dict[str, TheoryObservation] = {}
        for index, selection in enumerate(selected):
            if index == max_combinations:
                raise LimitExceeded(
                    "Selected combinations exceed the configured prefix"
                )
            reserve(operation, 16 + len(selection.components) + len(selection.bridges))
            try:
                for name in (*selection.components, *selection.bridges):
                    if name not in baselines:
                        baseline = observe_theory(
                            backend,
                            catalog[name],
                            classes,
                            queries,
                            operation,
                            heap_mb,
                            observations,
                        )
                        baselines[name] = baseline
                        reports.append(baseline.report)
                        retain_report(baseline.report, operation, issues)
                originals = tuple(baselines[name] for name in selection.components)
                unbridged_theory = assemble_theory(
                    derived_label(selection.label, ":unbridged", operation),
                    tuple(catalog[name] for name in selection.components),
                    operation,
                )
                unbridged = observe_theory(
                    backend,
                    unbridged_theory,
                    classes,
                    queries,
                    operation,
                    heap_mb,
                    observations,
                )
                reports.append(unbridged.report)
                comparison = compare_theories(originals, unbridged, operation)
                reports.append(comparison.report)
                interactions: list[TheoryInteraction] = []
                _explain_interactions(
                    backend,
                    unbridged_theory,
                    comparison,
                    "unbridged",
                    operation,
                    heap_mb,
                    max_explanations,
                    max_axioms,
                    proofs,
                    interactions,
                    reports,
                    issues,
                )
                joint = unbridged
                bridge_comparison = None
                if selection.bridges:
                    joint_theory = assemble_theory(
                        derived_label(selection.label, ":bridged", operation),
                        tuple(
                            catalog[name]
                            for name in (*selection.components, *selection.bridges)
                        ),
                        operation,
                    )
                    joint = observe_theory(
                        backend,
                        joint_theory,
                        classes,
                        queries,
                        operation,
                        heap_mb,
                        observations,
                    )
                    reports.append(joint.report)
                    bridge_comparison = compare_theories((unbridged,), joint, operation)
                    reports.append(bridge_comparison.report)
                    _explain_interactions(
                        backend,
                        joint_theory,
                        bridge_comparison,
                        "bridged",
                        operation,
                        heap_mb,
                        max_explanations,
                        max_axioms,
                        proofs,
                        interactions,
                        reports,
                        issues,
                    )
                logical = _logical_report(joint, operation)
                result = JointCompatibility(
                    selection,
                    unbridged,
                    joint,
                    comparison,
                    bridge_comparison,
                    tuple(interactions),
                    logical,
                )
                results.append(result)
                reports.append(logical)
                retain_report(logical, operation, issues)
            except LimitExceeded:
                raise
            except ResourceError as error:
                issues.append(
                    Issue(
                        "owl.integration_unverified",
                        str(error),
                        selection.label,
                        kind="unverified",
                    )
                )
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.integration")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.integration_unverified"
                    if unknown
                    else "owl.integration_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.integration")
    for report in reports:
        issues.retain_completed(report)
    complete = (
        finished
        and len(results) == requested
        and all(report.complete for report in reports)
    )
    return IntegrationCheck(
        tuple(observations),
        tuple(proofs),
        tuple(results),
        requested,
        complete,
        Report(
            "owl.integration",
            len(results),
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(reports),
            regime="explicit-selected-native-component-union-bridge-comparison",
        ),
    )
