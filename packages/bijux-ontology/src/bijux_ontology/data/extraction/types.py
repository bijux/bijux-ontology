"""Finite selected record paths and explicit description boundaries."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.data.projection.types import ReadField
from bijux_ontology.errors import InputError
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class RecordField:
    """Reuse raw read cardinalities and expand only explicitly listed child fields.

    Without children, resource endpoints remain reference-only. List fields retain
    their actual first/rest structure and apply children to members, not cells.
    """

    read: ReadField
    children: tuple["RecordField", ...] = ()

    def __post_init__(self) -> None:
        """Require finite typed raw intent; the operation owns recursive bounds."""
        if not isinstance(self.read, ReadField) or self.read.decoder != "raw":
            raise InputError("Extraction fields require a raw ReadField")
        if type(self.children) is not tuple or any(
            not isinstance(child, RecordField) for child in self.children
        ):
            raise InputError("Child descriptions require immutable RecordField tuples")


@dataclass(frozen=True)
class RecordReference:
    """Locate an original-source endpoint and its selected description policy.

    Output blank nodes belong to the fresh extracted source. These receipts
    retain input ownership and do not act as anonymous output selectors.
    """

    source: PreparedSource
    root: URIRef | BNode
    location: str
    node: Node
    graph: URIRef | BNode | None
    description: str


@dataclass(frozen=True)
class RecordExtraction:
    """A complete selected dataset or required coverage explaining its absence."""

    source: PreparedSource | None
    references: tuple[RecordReference, ...]
    report: Report


@dataclass(frozen=True)
class RecordPlan:
    """A qualified finite extraction plan, independently of SHACL validation."""

    fields: tuple[RecordField, ...]
    report: Report
