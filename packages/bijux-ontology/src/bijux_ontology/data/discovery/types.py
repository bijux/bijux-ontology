"""Finite observations remain separate from consumer-accepted output policies."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.paths import Path
from rdflib.term import Node

from bijux_ontology.data.projection.types import ReferenceHandle
from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.paths import RdfPath, capture_path
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class DiscoveryField:
    """Name one explicitly selected finite RDF path; no property is guessed."""

    name: str
    path: URIRef | Path | RdfPath

    def __post_init__(self) -> None:
        """Capture path intent before later caller mutation."""
        if type(self.name) is not str or not self.name.strip():
            raise InputError("Discovery fields require ordinary nonempty names")
        object.__setattr__(self, "path", capture_path(self.path))


@dataclass(frozen=True)
class FieldObservation:
    """Original endpoints and asserted type inventories for one selected record."""

    name: str
    endpoints: tuple[Node, ...]
    asserted_types: tuple[tuple[Node, ...], ...]
    alternatives: tuple[ValueDeclaration, ...]
    qualified: bool


@dataclass(frozen=True)
class RecordObservation:
    """Record coverage includes empty fields and uninterpreted endpoint evidence."""

    reference: ReferenceHandle
    fields: tuple[FieldObservation, ...]

    @property
    def subject(self) -> URIRef | BNode:
        """Retain source-owned record identity beside the exact physical graph."""
        return self.reference.subject


@dataclass(frozen=True)
class ObservedField:
    """Observed extrema are sample facts, never universal cardinality proofs."""

    field: DiscoveryField
    minimum: int
    maximum: int
    alternatives: tuple[ValueDeclaration, ...]
    declaration: PathFieldDeclaration | None


@dataclass(frozen=True, init=False, eq=False)
class ContractCandidate:
    """Reviewable proposals and exact corpus evidence, without application authority."""

    source: PreparedSource | None
    graph: URIRef | BNode | None
    records: tuple[RecordObservation, ...]
    fields: tuple[ObservedField, ...]
    shape: URIRef
    shapes: PreparedSource | None
    native_report: Report | None
    shape_report: Report | None
    report: Report

    def __init__(self) -> None:
        raise InputError("Use discover_contracts to qualify finite observations")

    @classmethod
    def _owned(
        cls,
        source: PreparedSource | None,
        graph: URIRef | BNode | None,
        records: tuple[RecordObservation, ...],
        fields: tuple[ObservedField, ...],
        shape: URIRef,
        shapes: PreparedSource | None,
        native_report: Report | None,
        shape_report: Report | None,
        report: Report,
    ) -> "ContractCandidate":
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("graph", graph),
            ("records", records),
            ("fields", fields),
            ("shape", shape),
            ("shapes", shapes),
            ("native_report", native_report),
            ("shape_report", shape_report),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True, init=False, eq=False)
class AcceptedContract:
    """An explicit consumer decision to enforce the observed candidate envelope.

    Acceptance establishes application policy, not population completeness,
    universal maxima, required source keys or closed ontology vocabulary.
    """

    candidate: ContractCandidate

    def __init__(self) -> None:
        raise InputError("Use accept_candidate with an explicit policy decision")

    @classmethod
    def _owned(cls, candidate: ContractCandidate) -> "AcceptedContract":
        result = object.__new__(cls)
        object.__setattr__(result, "candidate", candidate)
        return result
