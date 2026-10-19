"""Exact numbered membership and explicit closed application requirements."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.roles.types import Statement
from bijux_ontology.rdf.snapshots import PreparedSource


@dataclass(frozen=True)
class ContainerSlot:
    """One positive canonical decimal ordinal with all exact distinct RDF values.

    Decimal text preserves enormous ordinals without integer conversion. Numeric
    order is length followed by lexical order; repeated values in other slots
    remain separate occurrences. Original physical assertions are retained.
    """

    ordinal: str
    predicate: URIRef
    values: tuple[Node, ...]
    statements: tuple[Statement, ...]


@dataclass(frozen=True)
class ContainerGap:
    """Unobserved positive ordinals strictly between these compact bounds.

    None for after means before the first observed positive ordinal. A gap is
    observation metadata, never an RDF completeness error or invented member.
    """

    after: str | None
    before: str


@dataclass(frozen=True)
class ContainerRecord:
    """One original physical graph occurrence, with optional ambiguous slots."""

    node: URIRef | BNode
    graph: URIRef | BNode | None
    kinds: tuple[URIRef, ...]
    slots: tuple[ContainerSlot, ...]
    gaps: tuple[ContainerGap, ...]
    statements: tuple[Statement, ...]
    annotations: tuple[Statement, ...]


@dataclass(frozen=True)
class ContainerInspection:
    """Raw selected data; complete inspection does not imply a closed container."""

    source: PreparedSource | None
    records: tuple[ContainerRecord, ...]
    missing: tuple[URIRef | BNode, ...]
    report: Report


@dataclass(frozen=True)
class ContainerContract:
    """Closed sequence/bag policy explicitly selected by the application.

    Both require one value per slot; sequence additionally requires consecutive
    slots starting at one. Bag numbering identifies occurrences without requiring
    gaps to be filled or inferring an order. Optional size counts actual slots.
    These requirements are application choices, never RDF entailments.
    """

    kind: Literal["sequence", "bag"]
    size: int | None = None
    require_type: bool = True

    def __post_init__(self) -> None:
        """Reject implicit policies and oversized integer metadata before formatting."""
        if self.kind not in ("sequence", "bag") or type(self.require_type) is not bool:
            raise InputError("Select a sequence/bag contract and boolean type policy")
        if self.size is not None and (
            type(self.size) is not int or self.size < 0 or self.size.bit_length() > 4096
        ):
            raise InputError("Container sizes require bounded nonnegative integers")


@dataclass(frozen=True)
class ContainerCheck:
    """Actual inspection and selected application findings, preserving raw data."""

    inspection: ContainerInspection
    contract: ContainerContract
    report: Report


@dataclass(frozen=True)
class ContainerConversion:
    """Original container evidence and an ordinary checked copied list candidate."""

    check: ContainerCheck
    candidate: MigrationCandidate | None
    report: Report
