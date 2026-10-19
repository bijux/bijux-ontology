"""Scoped Data Cube structure, observation and individual integrity coverage."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource


@dataclass(frozen=True, slots=True)
class CubeComponent:
    """Original specification values retain malformed or conflicting declarations."""

    specification: Node
    properties: tuple[Node, ...]
    required: tuple[Node, ...]
    attachments: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class CubeStructure:
    """One supplied DSD and its explicitly associated component specifications."""

    identifier: URIRef | BNode
    components: tuple[CubeComponent, ...]


@dataclass(frozen=True, slots=True)
class CubeObservation:
    """Actual normalized values with their original subject and dataset links."""

    subject: URIRef | BNode
    datasets: tuple[Node, ...]
    values: tuple[tuple[URIRef, tuple[Node, ...]], ...]


@dataclass(frozen=True, slots=True)
class CubeConstraintCoverage:
    """Completion and applicability of one IC; absence is never an execution receipt."""

    identifier: str
    status: Literal[
        "complete", "not_applicable", "unverified", "exhausted", "not_executed"
    ]
    method: str
    report: Report | None


@dataclass(frozen=True, slots=True)
class CubeCheck:
    """Owned normalization and exact selected scope beside all integrity receipts."""

    report: Report
    normalized: PreparedSource | None = None
    structures: tuple[CubeStructure, ...] = ()
    observations: tuple[CubeObservation, ...] = ()
    coverage: tuple[CubeConstraintCoverage, ...] = ()
    datasets: tuple[URIRef | BNode, ...] = ()
    source_metadata: SnapshotMetadata | None = None
    graph_name: GraphSelection | None = None
