"""Independent native consistency and consumer proofs for each root transaction."""

from collections.abc import Mapping

from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget
from bijux_ontology.reports import Report

from ..axioms.changes.checking import prepare_axiom_change
from ..axioms.changes.preconditions import source_base
from ..axioms.changes.types import AxiomCheck
from ..axioms.types import ProjectedAxiom
from ..backend.types import OwlProfileBackend
from ..diagnosis.checking import check_consistency
from ..diagnosis.types import ConsistencyCheck
from ..documents.types import OwlDocument
from ..entailment.checking import check_entailments
from ..entailment.types import EntailmentCheck
from .preparation import reserve
from .types import RepairEdit, RepairProposal


def check_candidate(
    backend: OwlProfileBackend,
    source: OwlDocument,
    imports: Mapping[str, OwlDocument] | None,
    edits: tuple[RepairEdit, ...],
    obligations: tuple[ProjectedAxiom, ...],
    operation: Operation,
    heap_mb: int,
) -> RepairProposal:
    """Keep every paid native observation before its transaction adopts reports."""
    reserve(operation, len(edits) + sum(len(edit.additions) for edit in edits) + 8)
    consistency: list[ConsistencyCheck] = []
    entailments: list[EntailmentCheck] = []

    def consistent(candidate: OwlDocument, budget: Budget) -> Report:
        """Retain actual mature-reasoner truth, independently of policy selection."""
        result = check_consistency(
            backend,
            candidate,
            named_classes=(),
            imports=imports,
            budget=budget,
            heap_mb=heap_mb,
        )
        consistency.append(result)
        return result.report

    def consumers(candidate: OwlDocument, budget: Budget) -> Report:
        """Require all selected consumer consequences of this exact candidate."""
        result = check_entailments(
            backend,
            candidate,
            obligations,
            imports=imports,
            budget=budget,
            heap_mb=heap_mb,
        )
        entailments.append(result)
        return result.report

    additions = tuple(item for edit in edits for item in edit.additions)
    candidate = prepare_axiom_change(
        backend,
        source,
        expected=source_base(source),
        additions=additions,
        removals=tuple(edit.axiom for edit in edits),
        imports=imports,
        profiles=("owl2-dl",),
        after_checks=(
            AxiomCheck("candidate consistency", consistent),
            AxiomCheck("required consumer entailments", consumers),
        ),
        budget=operation,
        heap_mb=heap_mb,
    )
    accepted = None
    if (
        candidate.report.verified
        and consistency
        and consistency[0].consistent is True
        and consistency[0].report.verified
        and entailments
        and entailments[0].report.verified
    ):
        accepted = True
    elif candidate.report.complete and candidate.report.outcome == "disagreement":
        accepted = False
    return RepairProposal(
        edits,
        candidate,
        consistency[0] if consistency else None,
        entailments[0] if entailments else None,
        accepted,
        candidate.report,
    )
