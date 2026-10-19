"""Original annotation occurrences and explicit slim classification evidence."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.relation_paths.types import RelationPathResult


@dataclass(frozen=True)
class SlimAnnotation:
    """One original fine-grained annotation attached to an exact record ID."""

    record_id: str
    term: str


@dataclass(frozen=True)
class SlimMatch:
    """An applicable selected category and its actual typed path distance."""

    root: URIRef
    distance: int
    path: RelationPathResult


@dataclass(frozen=True)
class SlimTermAssignment:
    """Shared query evidence for one fine term, never a replacement for it.

    Applicable paths remain visible if coverage is incomplete. Selected matches
    are final only when every required root comparison completed. Nearest ties
    are all retained; distance counts selected typed edges, not inferred axioms.
    """

    term: URIRef
    status: Literal["covered", "uncovered", "unknown"]
    applicable: tuple[SlimMatch, ...]
    selected: tuple[SlimMatch, ...]
    comparisons: tuple[RelationPathResult, ...]


@dataclass(frozen=True)
class SlimAssignment:
    """One original occurrence with its shared fine-term classification."""

    index: int
    annotation: SlimAnnotation
    classification: SlimTermAssignment


@dataclass(frozen=True)
class SlimCount:
    """Count selected annotation occurrences and distinct exact record IDs."""

    root: URIRef
    annotations: int
    records: int


@dataclass(frozen=True)
class SlimAssignmentSet:
    """Finite corpus classification and exact counts for final selections.

    uncovered, unknown and multiple count annotation occurrences. multiple counts
    occurrences with at least two established applicable roots, even when the
    nearest policy selects only one. Counts never replace original fine terms.
    """

    roots: tuple[URIRef, ...]
    policy: Literal["nearest", "all"] | None
    assignments: tuple[SlimAssignment, ...]
    counts: tuple[SlimCount, ...]
    uncovered: int
    unknown: int
    multiple: int
    report: Report
