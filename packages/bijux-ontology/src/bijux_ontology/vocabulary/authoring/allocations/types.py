"""Explicit curator ownership and reviewable local allocation intents."""

from dataclasses import dataclass
from typing import Literal

from rdflib import Literal as RdfLiteral
from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.patches.types import SnapshotWitness
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry


def text(value: object, budget: Budget) -> str:
    """Own scalar metadata before hashing, splitting or formatting it."""
    if type(value) is not str:
        raise InputError("Allocation metadata requires plain text")
    bound_text(value, budget)
    if not value:
        raise InputError("Allocation metadata must be nonempty")
    return value


@dataclass(frozen=True)
class CuratorRange:
    """An inclusive numeric range under one explicitly contributed prefix."""

    owner: str
    prefix: str
    start: int
    end: int
    digits: int

    def __post_init__(self) -> None:
        """Reject unbounded numeric formatting and invalid intervals."""
        budget = Budget()
        text(self.owner, budget)
        text(self.prefix, budget)
        if (
            type(self.start) is not int
            or type(self.end) is not int
            or type(self.digits) is not int
            or not 1 <= self.digits <= 64
            or not 0 <= self.start <= self.end < 10**self.digits
        ):
            raise InputError("Allocation ranges need nonnegative padded integers")


@dataclass(frozen=True)
class AllocationPolicy:
    """Caller-owned numeric ranges and complete selected collision conventions.

    All named RDF occurrences are unavailable, not merely declared subjects.
    Alternate predicates identify accession literals or IRIs; unsupported required
    alternate recognition prevents initialization rather than implying absence.
    Explicit retired identifiers remain unavailable without surviving RDF facts.
    """

    registry: IdentifierRegistry
    ranges: tuple[CuratorRange, ...]
    reserved: tuple[URIRef, ...] = ()
    retired: tuple[URIRef, ...] = ()
    alternate_rules: tuple[AccessionRule, ...] = ()
    alternate_predicates: tuple[URIRef, ...] = (
        URIRef("http://www.geneontology.org/formats/oboInOwl#hasAlternativeId"),
    )


@dataclass(frozen=True)
class AllocationRequest:
    """A caller-stable term intent; kind selects only a named RDF declaration."""

    key: str
    kind: Literal["class", "concept", "scheme"] = "class"
    label: RdfLiteral | None = None

    def __post_init__(self) -> None:
        """Keep exact language/datatype/lexical label identity."""
        budget = Budget()
        text(self.key, budget)
        if self.kind not in ("class", "concept", "scheme"):
            raise InputError("Unsupported allocated resource declaration")
        if self.label is not None:
            if not isinstance(self.label, RdfLiteral):
                raise InputError("Allocated labels require RDF literals")
            bound_node(self.label, budget)


@dataclass(frozen=True)
class AllocationAssignment:
    """An exact proposed identifier and explicit term intent, never equivalence."""

    iri: URIRef
    request: AllocationRequest

    def __post_init__(self) -> None:
        """Reject guessed IRI intent and untyped allocation requests."""
        if not isinstance(self.iri, URIRef) or not isinstance(
            self.request, AllocationRequest
        ):
            raise InputError("Assignments require an IRI and AllocationRequest")
        bound_node(self.iri, Budget())
        absolute_iri(self.iri)


@dataclass(frozen=True)
class AllocationDraft:
    """Portable review evidence; importing it still requires fresh postchecks."""

    store_id: str
    revision: int
    owner: str
    prefix: str
    assignments: tuple[AllocationAssignment, ...]


@dataclass(frozen=True, init=False)
class AllocationSnapshot:
    """One loaded durable base and a fresh, independently owned RDF projection."""

    store_id: str
    revision: int
    source: PreparedSource
    witness: SnapshotWitness
    history: tuple[AllocationAssignment, ...]
    unavailable: frozenset[str]
    _policy: bytes
    _metadata: tuple[str | None, str | None, str]
    _receipt: bytes
    _scope: SnapshotMetadata

    def __init__(self) -> None:
        """Only a checked local store may create an allocation base."""
        raise InputError("Allocation snapshots must be loaded from AllocationStore")


@dataclass(frozen=True, init=False)
class AllocationProposal:
    """A draft plus checked native additions; allocation remains uncommitted."""

    draft: AllocationDraft | None
    candidate: MigrationCandidate | None
    report: Report
    _base: AllocationSnapshot | None

    def __init__(self) -> None:
        """Prevent unchecked proposal construction through the public constructor."""
        raise InputError("Allocation proposals must be prepared by AllocationStore")


@dataclass(frozen=True)
class AllocationCommit:
    """A committed revision only when both durable history and RDF changed."""

    revision: int | None
    report: Report
