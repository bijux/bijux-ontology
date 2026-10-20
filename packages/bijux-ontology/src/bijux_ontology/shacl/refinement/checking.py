"""Explicitly aligned finite refinement, with actual maintained data witnesses."""

import threading
from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.namespace import SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.analysis.checking import analyze_shape_profile
from bijux_ontology.shacl.authoring.types import ShaclNodeShape
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.inspection.reading import inspect_shapes
from bijux_ontology.shacl.refinement.normalization import Branch, Branches, Unsupported
from bijux_ontology.shacl.refinement.proofs import prove
from bijux_ontology.shacl.refinement.types import (
    ProfileRefinement,
    RefinementContract,
    RefinementProof,
)
from bijux_ontology.shacl.refinement.witnesses import candidate, run


@dataclass(frozen=True)
class _Prepared:
    """Only original inspected definitions and explicit target identities."""

    branches: tuple[Branch, ...]
    targets: frozenset[tuple[object, ...]]
    focus: URIRef
    classes: tuple[str, ...]


def _prepare(
    contract: RefinementContract,
    budget: Operation,
    issues: IssueCollector,
    reports: list[Report],
) -> _Prepared:
    """Qualify source ownership and supported remainder before any proof."""
    bound_node(contract.root, budget)
    if contract.data_graph is not None:
        bound_node(contract.data_graph, budget)
    if contract.source.literal_fidelity not in ("source_terms", "supplied_terms"):
        raise Unsupported("Refinement requires preserved supplied RDF terms")
    if contract.source.union:
        raise Unsupported("Refinement shapes require one selected physical graph")
    graph = contract.source._read(budget=budget)
    if isinstance(contract.root, BNode):
        owned = False
        for triple in graph:
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
                owned = owned or node is contract.root
        if not owned:
            raise InputError("Blank refinement roots require exact source ownership")
    inspection = inspect_shapes(contract.source, (contract.root,), budget=budget)
    analysis = analyze_shape_profile(inspection.profile, budget=budget)
    retain_report(analysis.report, budget, issues)
    reports.append(analysis.report)
    if not analysis.report.verified or any(c.recursive for c in analysis.components):
        raise Unsupported("Incomplete, contradictory or recursive profile analysis")
    # Deactivation is deliberately opaque to typed authoring but is qualified by
    # the existing analysis over the actual RDF. No other remainder is discharged.
    budget.consume("states", len(inspection.profile.dependencies) * 2 + 1)
    nodes: set[Node] = {contract.root}
    for edge in inspection.profile.dependencies:
        budget.consume("work")
        nodes.update((edge.shape, edge.dependency))
    if len(inspection.definitions) != len(nodes) or any(
        item.predicate != SH.deactivated for item in inspection.remainder
    ):
        raise Unsupported(
            "Selected semantic constraints remain outside finite refinement"
        )
    if not inspection.profile.structural_report.verified:
        raise Unsupported("Selected shape closure is incomplete")
    budget.consume("work", len(inspection.definitions))
    definition = next(
        (item for item in inspection.definitions if item.identifier == contract.root),
        None,
    )
    if not isinstance(definition, ShaclNodeShape):
        raise Unsupported("Refinement roots require supported node shapes")
    budget.consume(
        "states", len(definition.target_nodes) + len(definition.target_classes)
    )
    budget.consume(
        "work", len(definition.target_nodes) + len(definition.target_classes)
    )
    targets = {("node", *term_key(value)) for value in definition.target_nodes}
    targets.update(("class", value) for value in definition.target_classes)
    if not targets or any(
        not isinstance(node, URIRef) for node in definition.target_nodes
    ):
        raise Unsupported("Refinement requires nonempty named node or class targets")
    focus = (
        definition.target_nodes[0]
        if definition.target_nodes
        else URIRef("urn:bijux:ontology:refinement:focus")
    )
    assert isinstance(focus, URIRef)
    branches = Branches(
        inspection.definitions, analysis.deactivated_shapes, budget
    ).expand(contract.root)
    return _Prepared(branches, frozenset(targets), focus, definition.target_classes)


def _adopt(result: ShaclResult, budget: Operation) -> bool:
    """Bound raw expected-negative evidence without turning it into an API finding."""
    retained = IssueCollector(budget)
    try:
        retain_report(result.report, budget, retained)
    except LimitExceeded as error:
        retained.exhausted(str(error))
        result.report = Report(
            "shacl-refinement-witness",
            issues=tuple(retained),
            requested=1,
            complete=False,
        )
    return result.report.complete and result.report.outcome in (
        "agreement",
        "disagreement",
    )


def compare_shape_refinement(
    parent: RefinementContract,
    child: RefinementContract,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ProfileRefinement:
    """Compare a finite SHACL subset before any mandatory profile composition.

    Supports counts, exact fixed/enumerated terms, datatype/node-kind and acyclic
    node AND/OR over equal captured paths. This sufficient implication calculus
    is not general SHACL implication. Target/data scope must match exactly.
    A positive universal proof additionally requires actual compatible data;
    weakening requires maintained child-pass/parent-fail evidence. Unsupported
    constructs or unsuccessful finite witness attempts remain unknown.
    """
    if not isinstance(parent, RefinementContract) or not isinstance(
        child, RefinementContract
    ):
        raise InputError("Refinement comparison requires two explicit contracts")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    proofs: tuple[RefinementProof, ...] = ()
    witness: PreparedSource | None = None
    parent_result: ShaclResult | None = None
    child_result: ShaclResult | None = None
    relation: Literal["refines", "weakens", "unknown"] = "unknown"
    completed = 0
    try:
        first = _prepare(parent, operation, issues, reports)
        second = _prepare(child, operation, issues, reports)
        operation.consume("work", len(first.targets) + len(second.targets))
        if first.targets != second.targets or parent.data_graph != child.data_graph:
            raise Unsupported("Parent and child targets or dataset scopes differ")
        proofs = prove(first.branches, second.branches, operation)
        universal = len(proofs) == len(second.branches)
        for branch in second.branches:
            # Two deterministic choices expose ordinary omission/enum weakening;
            # this finite search is never interpreted as an implication proof.
            for alternative in range(2):
                operation.consume("work")
                witness = candidate(
                    branch, child, second.focus, second.classes, alternative, operation
                )
                child_result = run(child, witness, operation)
                if not _adopt(child_result, operation):
                    retain_report(child_result.report, operation, issues)
                    raise Unsupported("Actual child witness validation is incomplete")
                if (
                    not child_result.report.verified
                    or child_result.conforms is not True
                ):
                    continue
                parent_result = run(parent, witness, operation)
                if not _adopt(parent_result, operation):
                    retain_report(parent_result.report, operation, issues)
                    raise Unsupported("Actual parent witness validation is incomplete")
                completed += 1
                if parent_result.conforms is False:
                    relation = "weakens"
                    issues.append(
                        Issue(
                            "shacl.profile_weakening",
                            "Actual data conforms to the proposed child "
                            "and violates its parent",
                        )
                    )
                    break
                if (
                    universal
                    and parent_result.report.verified
                    and parent_result.conforms is True
                ):
                    relation = "refines"
                    break
            if relation != "unknown":
                break
        if relation == "unknown":
            raise Unsupported(
                "No universal finite proof with compatibility "
                "or actual weakening witness was established"
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError) as error:
        try:
            issues.append(
                Issue(
                    "shacl.profile_refinement_unverified",
                    str(error),
                    kind="unverified",
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    report = Report(
        "shacl-profile-refinement",
        completed,
        tuple(issues),
        requested=max(1, completed),
        complete=relation != "unknown",
        children=tuple(reports),
        regime="finite-profile-refinement",
    )
    return ProfileRefinement(
        parent, child, relation, proofs, witness, parent_result, child_result, report
    )
