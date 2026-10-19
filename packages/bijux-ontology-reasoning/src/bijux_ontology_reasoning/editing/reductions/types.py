"""Counterfactual redundancy observations over exact owned root assertions."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report

from ...axioms.changes.types import AxiomCandidate
from ...axioms.types import ProjectedAxiom
from ...documents.types import OwlDocument


@dataclass(frozen=True)
class RedundancyCheck:
    """One original assertion tested against its exact counterfactual theory."""

    axiom: ProjectedAxiom
    redundant: bool | None
    candidate: AxiomCandidate


@dataclass(frozen=True)
class RedundancyAnalysis:
    """Independent proposals, never authorization to remove their union.

    Nonredundant assertions retain their raw required-axiom findings. Inspect the
    typed checks and proposals; report agreement means every selected assertion
    was proved redundant, rather than just that the analysis finished.
    """

    original: OwlDocument | None
    checks: tuple[RedundancyCheck, ...]
    proposals: tuple[ProjectedAxiom, ...]
    report: Report
