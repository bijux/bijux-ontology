"""Finite counterfactual edits with original and candidate proof ownership."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.reports import Report

from ...axioms.changes.types import AxiomCandidate
from ...axioms.types import ProjectedAxiom
from ...documents.types import OwlDocument
from ...inference.consistency.types import ConsistencyCheck
from ...inference.entailment.types import EntailmentCheck
from ...inference.explanations.types import ExplanationCheck, SourceAxiom


@dataclass(frozen=True)
class AllowedReplacement:
    """Caller-authored meaning, never an automatically selected weakening."""

    origin: SourceAxiom
    additions: tuple[ProjectedAxiom, ...]
    label: str


@dataclass(frozen=True)
class RepairEdit:
    """One exact annotated root assertion removed or explicitly replaced."""

    origin: SourceAxiom
    axiom: ProjectedAxiom
    additions: tuple[ProjectedAxiom, ...]
    label: str


@dataclass(frozen=True)
class RepairProposal:
    """An inspectable candidate, including rejected or incompletely checked work."""

    edits: tuple[RepairEdit, ...]
    candidate: AxiomCandidate
    consistency: ConsistencyCheck | None
    entailments: EntailmentCheck | None
    accepted: bool | None
    report: Report


@dataclass(frozen=True)
class RepairSearch:
    """Checked finite alternatives, without authorization to choose their meaning.

    Search completion covers only combinations of editable origins in the one
    established justification, within max_removals and authored alternatives.
    It never enumerates every justification or every repair of the ontology.
    Rejected candidate findings and the original inconsistency remain in their
    own raw records; report describes qualification of the configured search.
    """

    original: OwlDocument | None
    imports: tuple[tuple[str, OwlDocument], ...]
    explanation: ExplanationCheck | None
    protected_axioms: tuple[SourceAxiom, ...]
    alternatives: tuple[RepairProposal, ...]
    max_candidates: int
    max_removals: int
    search_complete: bool
    report: Report
    enumeration_complete: bool = False
    minimality: Literal["neither"] = "neither"

    @property
    def proposals(self) -> tuple[RepairProposal, ...]:
        """Expose only individually verified candidates, never their union."""
        return tuple(item for item in self.alternatives if item.accepted is True)
