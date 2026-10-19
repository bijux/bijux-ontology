"""Global emitted RDF consistency and independently established record scopes."""

from __future__ import annotations

from dataclasses import dataclass

from rdflib import BNode, URIRef

from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.sources.types import GraphKind

from ..consistency.types import ConsistencyCheck


@dataclass(frozen=True)
class EmittedScope:
    """An unchanged original snapshot and its one explicitly selected context."""

    source: PreparedSource
    graph_kind: GraphKind
    graph_name: URIRef | BNode | None
    statements: int


@dataclass(frozen=True)
class EmittedRecord:
    """Source-node participation, never an individual consistency or guilt verdict."""

    binding: RdfModelUsage
    participating_statements: int | None
    report: Report


@dataclass(frozen=True)
class EmittedConsistency:
    """Joint native diagnosis, original scopes and separate attribution coverage.

    Every selected emitted statement participates in reasoning. Bindings are
    diagnostic anchors, not an implicit subject filter. Submission conformance
    and explanations are separate operations.
    """

    ontology: EmittedScope | None
    emitted: EmittedScope | None
    imports: tuple[PreparedSource, ...]
    derived: PreparedSource | None
    records: tuple[EmittedRecord, ...]
    diagnosis: ConsistencyCheck | None
    attribution_report: Report
    report: Report
