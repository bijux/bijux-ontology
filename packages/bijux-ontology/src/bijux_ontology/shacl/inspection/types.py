"""Typed views and explicit count edits beside the original authored RDF."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
)
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.profiles.types import ShapeProfile


@dataclass(frozen=True)
class ShapeRemainder:
    """An original semantic assertion absent from the supported typed view."""

    subject: URIRef | BNode
    predicate: URIRef
    value: Node


@dataclass(frozen=True)
class ShapeInspection:
    """A scoped typed projection retaining its original immutable input.

    The source remains authoritative for annotations, extensions and exact RDF
    spellings. Definitions cover only the explicitly selected shape closure.
    """

    source: PreparedSource
    graph_name: GraphSelection | None
    profile: ShapeProfile
    definitions: tuple[ShaclNodeShape | ShaclPropertyShape, ...]
    remainder: tuple[ShapeRemainder, ...]
    report: Report


@dataclass(frozen=True)
class ShapeCountEdit:
    """Replace only one selected property shape's ordinary count parameters."""

    shape: URIRef | BNode
    count: ShaclCount


@dataclass(frozen=True)
class ShapeEdit:
    """A copied RDF candidate and its actual maintained validation evidence."""

    source: PreparedSource | None
    validation: ShaclResult | None
    report: Report
