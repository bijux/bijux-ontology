"""Finite relation languages keep asserted edge roles and witnesses distinct."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection
from bijux_ontology.vocabulary.results import Edge

Role = Literal["class", "skos", "existential", "asserted"]


@dataclass(frozen=True)
class RelationStep:
    """Select one exact predicate in one explicitly chosen interpretation."""

    predicate: str
    role: Role


@dataclass(frozen=True)
class RelationSequence:
    """Require every selected subpath in the supplied order."""

    paths: tuple["RelationSpec", ...]


@dataclass(frozen=True)
class RelationChoice:
    """Permit one of the supplied subpaths, without mixing their transitions."""

    paths: tuple["RelationSpec", ...]


@dataclass(frozen=True)
class RelationInverse:
    """Reverse navigation, without claiming an OWL inverse-property entailment."""

    path: "RelationSpec"


@dataclass(frozen=True)
class RelationRepeat:
    """Repeat a subpath with explicit finite minimum and optional upper bound."""

    path: "RelationSpec"
    minimum: int = 0
    maximum: int | None = None


RelationSpec = (
    RelationStep | RelationSequence | RelationChoice | RelationInverse | RelationRepeat
)


@dataclass(frozen=True)
class RelationEndpoint:
    """Conjoin selected identities, asserted declaration kinds and membership.

    Each nonempty identity/kind tuple is a set of alternatives. Kinds are exact
    supplied declarations, not entailed class membership. An open or incomplete
    enumeration cannot qualify complete path-contract coverage.
    """

    identifiers: tuple[str, ...] = ()
    declared_kinds: tuple[str, ...] = ()
    enumeration: ValueEnumeration | None = None


@dataclass(frozen=True)
class RelationWitness:
    """One oriented edge with its original authored assertion or restriction."""

    source: URIRef
    target: URIRef
    relation: RelationStep
    inverse: bool
    evidence: Edge | RestrictionProjection


@dataclass(frozen=True)
class RelationPathResult:
    """An actual witness, complete selected nonmatch, or incomplete search."""

    status: Literal["found", "nonmatch", "incomplete"]
    source: URIRef | None
    endpoint: URIRef | None
    steps: tuple[RelationWitness, ...]
    report: Report
