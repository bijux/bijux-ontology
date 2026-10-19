"""Explicit logical alignment with independent component and union baselines."""

from collections.abc import Sequence
from threading import Event

from rdflib import URIRef

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.mappings.composition.checking import _input
from bijux_ontology.vocabulary.mappings.types import MappingRead, MappingSet

from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.inspection import inspect_axioms
from ...axioms.types import AxiomInspection, ProjectedAxiom
from ...backend.types import OwlProfileBackend
from ...documents.sources.capture import document_receipt
from ...documents.types import OwlDocument
from ...inference.consistency.checking import _classes
from ...inference.entailment.encoding import encode_axiom
from ...inference.explanations.checking import explain_reasoning
from ...inference.explanations.types import ExplanationCheck, ExplanationQuestion
from ..comparison import compare_theories
from ..observations import logical_report, observe_theory
from ..sources import assemble_theory, capture_theories, reserve
from ..types import (
    TheoryCombination,
    TheoryComparison,
    TheoryComponent,
    TheoryObservation,
)
from .preparation import (
    build_bridge,
    capture_interpretations,
    declared_roles,
    interpretable,
    roles_match,
)
from .types import (
    BridgeAttribution,
    BridgeInterpretation,
    MappingBridge,
    MappingBridgeCheck,
)


def _same_source(left: OwlDocument, right: OwlDocument, operation: Operation) -> bool:
    """Pay exact original-byte/identity comparisons before source-owner matching."""
    operation.consume(
        "work",
        len(left.content)
        + len(right.content)
        + sum(
            len(text)
            for source in (left, right)
            for text in (
                source.format,
                source.document_iri,
                source.effective_url,
                source.sha256,
            )
        ),
    )
    return document_receipt(left) == document_receipt(right)


def _explain(
    backend: OwlProfileBackend,
    joint: TheoryComponent,
    comparison: TheoryComparison,
    bridges: list[MappingBridge],
    operation: Operation,
    heap_mb: int,
    max_explanations: int,
    max_axioms: int,
    proofs: list[ExplanationCheck],
    attributions: list[BridgeAttribution],
    reports: list[Report],
    issues: IssueCollector,
) -> None:
    """Correlate reproduced original source owners, never renderer text or scores."""
    reserve(operation, len(comparison.newly_unsatisfiable) + len(bridges) + 2)
    questions = (
        (ExplanationQuestion("inconsistency"),)
        if comparison.newly_inconsistent is True
        else tuple(
            ExplanationQuestion("unsatisfiability", class_iri=str(iri))
            for iri in comparison.newly_unsatisfiable
        )
    )
    for index, question in enumerate(questions):
        if index >= max_explanations:
            raise LimitExceeded("Selected bridge explanations exceed their bound")
        reserve(operation, 8)
        proof = explain_reasoning(
            backend,
            joint.source,
            question,
            imports=joint.imports,
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
            origins = tuple(
                origin
                for axiom in proof.justification.axioms
                for origin in axiom.origins
            )
            reserve(operation, len(origins) * (len(bridges) + 1) + 1)
            selected = []
            for bridge in bridges:
                module = bridge.module
                if module is not None and any(
                    _same_source(origin.document, module.source, operation)
                    for origin in origins
                ):
                    selected.append(bridge.interpretation)
            attributions.append(BridgeAttribution(proof, origins, tuple(selected)))
            if not selected:
                issues.append(
                    Issue(
                        "owl.bridge_attribution_unverified",
                        "Reproduced support has no established approved bridge "
                        "source owner",
                        kind="unverified",
                    )
                )
        retain_report(proof.explanation_report, operation, issues)


def check_mapping_bridges(
    backend: OwlProfileBackend,
    mappings: MappingSet | MappingRead,
    subject: TheoryComponent,
    object_: TheoryComponent,
    interpretations: Sequence[BridgeInterpretation],
    *,
    protected_entailments: Sequence[ProjectedAxiom],
    named_classes: Sequence[URIRef] | None = None,
    max_explanations: int = 8,
    max_axioms: int = 64,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> MappingBridgeCheck:
    """Check caller-approved finite class/property meanings of exact mapping rows.

    SKOS relations and confidence do not select any OWL meaning. Each approved
    occurrence must have unambiguous named endpoints and declared original roles;
    unsupported selected qualifiers remain required unknown. Property equivalence
    is two explicitly selected subproperty inclusions. Imports stay read-only.

    Actual original observations precede an unbridged union, which is the baseline
    for bridge-induced changes. Existing source or union failures are retained,
    never blamed on an irrelevant bridge. Selected joint class truth and nonempty
    protected consumer entailments are required. Actual independently reproduced
    support identifies candidate original rows, without unique causal ownership,
    automatic domain choices or exhaustive compatibility claims.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    mapping_set = None
    selected: tuple[BridgeInterpretation, ...] = ()
    inspections: list[AxiomInspection] = []
    bridges: list[MappingBridge] = []
    observations: list[TheoryObservation] = []
    proofs: list[ExplanationCheck] = []
    attributions: list[BridgeAttribution] = []
    reports: list[Report] = []
    component_comparison = bridge_comparison = None
    finished = False
    try:
        if any(
            type(value) is not int or value <= 0
            for value in (max_explanations, max_axioms)
        ):
            raise InputError("Bridge proof bounds require positive integers")
        classes = _classes(named_classes, operation)
        queries, _ = ConstructionEncoder(operation).axioms(protected_entailments)
        if not queries:
            raise InputError("Select nonempty required consumer entailments")
        for query in queries:
            encode_axiom(query, operation)
        # Reserve the producer reference before shared input adoption can stop.
        # The shared helper's local child list otherwise publishes it only after
        # retaining metadata, which may already have paid a diagnostic prefix.
        if isinstance(mappings, MappingRead):
            reserve(operation, 2)
            reports.append(mappings.report)
        mapping_set, metadata_unknown = _input(
            mappings,
            operation,
            issues,
            [] if isinstance(mappings, MappingRead) else reports,
        )
        if mapping_set is None:
            raise ResourceError("The selected mapping producer did not complete")
        selected = capture_interpretations(interpretations, mapping_set, operation)
        if type(subject) is not TheoryComponent or type(object_) is not TheoryComponent:
            raise InputError(
                "Bridge endpoints require two explicit native theory components"
            )
        catalog, _ = capture_theories(
            (subject, object_),
            (TheoryCombination("mapping-alignment", (subject.label, object_.label)),),
            operation,
        )
        originals = []
        for name in (subject.label, object_.label):
            reserve(operation, 4)
            original = observe_theory(
                backend,
                catalog[name],
                classes,
                queries,
                operation,
                heap_mb,
                observations,
            )
            originals.append(original)
            reports.append(original.report)
            retain_report(original.report, operation, issues)
        unbridged_theory = assemble_theory(
            "mapping-alignment:unbridged", tuple(catalog.values()), operation
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
        component_comparison = compare_theories(tuple(originals), unbridged, operation)
        reports.append(component_comparison.report)
        joint, joint_theory = unbridged, unbridged_theory
        if selected:
            roles = []
            for name in (subject.label, object_.label):
                reserve(operation, 4)
                component = catalog[name]
                inspection = inspect_axioms(
                    backend,
                    component.source,
                    imports=component.imports,
                    axiom_types=("Declaration",),
                    budget=operation,
                    heap_mb=heap_mb,
                )
                inspections.append(inspection)
                reports.append(inspection.report)
                retain_report(inspection.report, operation, issues)
                if not inspection.report.verified:
                    raise ResourceError(
                        "Original endpoint roles were not completely inspected"
                    )
                roles.append(declared_roles(inspection, operation))
            usable = True
            for index, interpretation in enumerate(selected):
                reserve(operation, 8)
                if not interpretable(
                    interpretation, mapping_set, metadata_unknown, operation
                ) or not roles_match(interpretation, roles[0], roles[1], operation):
                    usable = False
                    issues.append(
                        Issue(
                            "owl.bridge_semantics_unverified",
                            "Selected row qualifiers or original endpoint roles "
                            "are outside the finite bridge profile",
                            "mapping-row:" + str(interpretation.occurrence.index),
                            kind="unverified",
                        )
                    )
                    continue
                bridge = build_bridge(
                    backend, interpretation, index, operation, heap_mb
                )
                bridges.append(bridge)
                reports.append(bridge.candidate.report)
                retain_report(bridge.candidate.report, operation, issues)
                usable = usable and bridge.module is not None
            if not usable:
                raise ResourceError(
                    "Every selected bridge must have checked native meaning "
                    "before joint acceptance"
                )
            reserve(operation, len(bridges) + 2)
            modules = tuple(
                bridge.module for bridge in bridges if bridge.module is not None
            )
            joint_theory = assemble_theory(
                "mapping-alignment:bridged", (*catalog.values(), *modules), operation
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
            _explain(
                backend,
                joint_theory,
                bridge_comparison,
                bridges,
                operation,
                heap_mb,
                max_explanations,
                max_axioms,
                proofs,
                attributions,
                reports,
                issues,
            )
        logical = logical_report(joint, operation)
        reports.append(logical)
        retain_report(logical, operation, issues)
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.mapping_bridges")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.bridge_unverified" if unknown else "owl.bridge_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.mapping_bridges")
    for report in reports:
        issues.retain_completed(report)
    # A native observer may publish a paid partial current observation before it
    # can return to this driver; retain only that observation's paid identities.
    for observation in observations:
        issues.retain_completed(observation.report)
    complete = (
        finished
        and all(report.complete for report in reports)
        and not any(issue.kind != "finding" for issue in issues)
    )
    return MappingBridgeCheck(
        mapping_set,
        selected,
        tuple(inspections),
        tuple(bridges),
        tuple(observations),
        component_comparison,
        bridge_comparison,
        tuple(proofs),
        tuple(attributions),
        complete,
        Report(
            "owl.mapping_bridges",
            int(finished),
            tuple(issues),
            requested=1,
            complete=complete,
            children=tuple(reports),
            regime="caller-approved-native-mapping-bridges-with-unbridged-baseline",
        ),
    )
