"""Bounded established-support alternatives, without automatic domain choices."""

from collections.abc import Iterator, Mapping, Sequence
from itertools import combinations, product
from threading import Event

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.reports import Issue, Report

from ..axioms.types import ProjectedAxiom
from ..backend.types import OwlProfileBackend
from ..documents.sources.capture import (
    capture_document,
    capture_imports,
    document_receipt,
)
from ..documents.types import OwlDocument
from ..explanations.checking import explain_reasoning
from ..explanations.types import ExplanationCheck, ExplanationQuestion, SourceAxiom
from .checking import check_candidate
from .preparation import OriginBridge, controls, reserve
from .types import AllowedReplacement, RepairEdit, RepairProposal, RepairSearch


def _frontier(
    groups: tuple[tuple[RepairEdit, ...], ...],
    maximum: int,
    operation: Operation,
) -> Iterator[tuple[RepairEdit, ...]]:
    """Enumerate only the declared finite edit scope without allocating its powerset."""
    for size in range(1, min(maximum, len(groups)) + 1):
        reserve(operation, size + 1)
        for chosen in combinations(groups, size):
            reserve(operation, size + sum(len(group) for group in chosen))
            for alternative in product(*chosen):
                reserve(operation, size + 1)
                yield alternative


def _groups(
    explanation: ExplanationCheck,
    source: OwlDocument,
    protected: tuple[SourceAxiom, ...],
    replacements: tuple[AllowedReplacement, ...],
    bridge: OriginBridge,
) -> tuple[tuple[RepairEdit, ...], ...]:
    """Exclude imported and protected structures, retaining exact annotated origins."""
    operation = bridge.operation
    justification = explanation.justification
    if justification is None:
        raise ResourceError("An established native justification is required")
    protected_structures = tuple(
        (document_receipt(value.document), bridge.resolve(value)) for value in protected
    )
    for replacement in replacements:
        bridge.resolve(replacement.origin)
        if document_receipt(replacement.origin.document) != document_receipt(source):
            raise InputError("Authored replacements cannot edit read-only imports")
        if (
            document_receipt(replacement.origin.document),
            bridge.resolve(replacement.origin),
        ) in protected_structures:
            raise InputError("Authored replacements cannot edit protected assertions")
    reserve(operation, len(justification.axioms) + 1)
    roots: list[SourceAxiom] = []
    seen: set[SourceAxiom] = set()
    for support in justification.axioms:
        for value in support.origins:
            reserve(operation)
            # Every support owner is qualified even when it cannot be edited.
            bridge.resolve(value)
            if (
                document_receipt(value.document) == document_receipt(source)
                and value not in seen
            ):
                seen.add(value)
                roots.append(value)
    operation.consume("work", sum(len(value.rendering) + 1 for value in roots))
    roots.sort(key=lambda value: value.sha256)
    for replacement in replacements:
        if replacement.origin not in seen:
            raise InputError(
                "Authored replacements must select established support origins"
            )
    groups = []
    for value in roots:
        operation.consume("work", len(protected) + len(replacements) + 1)
        axiom = bridge.resolve(value)
        if (document_receipt(value.document), axiom) in protected_structures:
            continue
        reserve(operation, 1 + len(replacements))
        edits = [RepairEdit(value, axiom, (), "remove exact source assertion")]
        for authored in replacements:
            if authored.origin == value:
                edits.append(
                    RepairEdit(value, axiom, authored.additions, authored.label)
                )
        groups.append(tuple(edits))
    return tuple(groups)


def propose_repairs(
    backend: OwlProfileBackend,
    source: OwlDocument,
    *,
    consumer_entailments: Sequence[ProjectedAxiom],
    protected_axioms: Sequence[SourceAxiom] = (),
    allowed_replacements: Sequence[AllowedReplacement] = (),
    imports: Mapping[str, OwlDocument] | None = None,
    max_candidates: int = 8,
    max_removals: int = 1,
    max_axioms: int = 64,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> RepairSearch:
    """Check finite alternatives from one established inconsistency justification.

    Imports are read-only. Protected exact annotated assertions are not edited.
    All other annotations, headers and structures follow the native transaction
    preservation contract. Each candidate requires actual DL consistency and
    every selected nonempty consumer entailment; no original inconsistency can
    supply vacuous success. Caller-authored replacements supply domain meaning.

    search_complete covers this support's combinations through max_removals,
    with the supplied replacement choices. max_candidates is a checked prefix
    limit. Neither global enumeration, minimality, a preferred repair nor joint
    safety of separately checked alternatives is claimed. No output is published.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    original = None
    supplied = None
    selected: tuple[SourceAxiom, ...] = ()
    explanation = None
    alternatives: list[RepairProposal] = []
    reports: list[Report] = []
    finished = False
    applicable = False
    try:
        for value in (max_candidates, max_removals, max_axioms):
            if type(value) is not int or value <= 0:
                raise InputError("Repair search bounds require positive integers")
        original = capture_document(source, operation)
        supplied = capture_imports(imports, operation)
        selected, replacements, obligations = controls(
            protected_axioms, allowed_replacements, consumer_entailments, operation
        )
        reserve(operation, 8)
        explanation = explain_reasoning(
            backend,
            original,
            ExplanationQuestion("inconsistency"),
            imports=supplied,
            max_axioms=max_axioms,
            budget=operation,
            heap_mb=heap_mb,
        )
        reports.append(explanation.explanation_report)
        retain_report(explanation.explanation_report, operation, issues)
        if (
            explanation.applicable is not True
            or explanation.justification is None
            or not explanation.explanation_report.verified
            or not explanation.justification.reproduced
        ):
            raise ResourceError(
                "Repair search requires established original inconsistency"
            )
        applicable = True
        bridge = OriginBridge(
            backend, original, supplied, operation, heap_mb, issues, reports
        )
        groups = _groups(explanation, original, selected, replacements, bridge)
        for edits in _frontier(groups, max_removals, operation):
            if len(alternatives) == max_candidates:
                raise LimitExceeded(
                    "Repair search reached its configured candidate prefix"
                )
            # Reserve both proposal publication and raw proof references BEFORE
            # the transaction. A paid candidate is appended without later debit.
            reserve(operation, 8 + len(edits))
            proposal = check_candidate(
                backend, original, supplied, edits, obligations, operation, heap_mb
            )
            alternatives.append(proposal)
            if proposal.accepted is None:
                issues.retain_completed(proposal.report)
                raise ResourceError("A required candidate postcheck did not complete")
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.repairs")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.repairs_unverified" if unknown else "owl.repairs_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.repairs")
    for report in reports:
        issues.retain_completed(report)
    return RepairSearch(
        original,
        () if supplied is None else tuple(supplied.items()),
        explanation,
        selected,
        tuple(alternatives),
        max_candidates,
        max_removals,
        finished,
        Report(
            "owl.repairs",
            int(applicable) + len(alternatives),
            tuple(issues),
            requested=int(applicable) + len(alternatives) + int(not finished),
            complete=finished,
            children=tuple(reports),
            regime="one-established-support-explicit-root-counterfactuals",
        ),
    )
