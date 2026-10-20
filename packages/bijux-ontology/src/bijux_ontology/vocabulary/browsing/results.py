"""Typed evidence returned by vocabulary inspection and navigation."""

from dataclasses import dataclass
from typing import Generic, TypeVar
from typing import Literal as Direction

from rdflib import Literal, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Report

T = TypeVar("T")


@dataclass(frozen=True)
class Selection(Generic[T]):
    """Selected items and honest completion status."""

    items: tuple[T, ...]
    report: Report


@dataclass(frozen=True)
class Edge:
    """One asserted RDF statement with its selected source ownership."""

    subject: Node
    predicate: URIRef
    object: Node
    scope: str


@dataclass(frozen=True)
class TermInfo:
    """Exact identifier, supplied declaration kinds and source-scope evidence."""

    iri: str
    status: Direction["declared", "referenced", "absent"]
    kinds: tuple[str, ...]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Annotation:
    """Literal annotation with its predicate, language, datatype and source."""

    predicate: str
    value: str
    language: str | None
    datatype: str | None
    scope: str
    term: Literal


@dataclass(frozen=True)
class StructuralView:
    """Named assertions and anonymous expressions retain their distinct roles."""

    named: tuple[Edge, ...]
    anonymous: tuple[Edge, ...]
    characteristics: tuple[str, ...]
    unsupported: tuple[str, ...]
    report: Report


@dataclass(frozen=True)
class LabelSelection:
    """Equal-ranked preferred labels alongside every retained alternative."""

    preferred: tuple[Annotation, ...]
    alternatives: tuple[Annotation, ...]
    report: Report


@dataclass(frozen=True)
class SearchMatch:
    """Exact matching evidence with an ordering rank, without confidence claims."""

    term: TermInfo
    field: str
    value: str
    language: str | None
    scope: str
    rank: int


@dataclass(frozen=True)
class Deprecation:
    """Asserted state and replacement hints, without choosing migration semantics."""

    state: Direction["deprecated", "active", "absent", "conflicting", "unverified"]
    assertions: tuple[Edge, ...]
    replacements: tuple[Edge, ...]
    report: Report


@dataclass(frozen=True)
class Description:
    """A composed term view retaining all asserted metadata and relationships."""

    term: TermInfo
    labels: tuple[Annotation, ...]
    metadata: tuple[Annotation, ...]
    relationships: tuple[Edge, ...]
    deprecation: Deprecation
    report: Report


@dataclass(frozen=True)
class Summary:
    """Counts describe selected supplied facts, not the complete imported world."""

    scopes: tuple[str, ...]
    statements: int
    declared: int
    referenced_only: int
    kinds: tuple[tuple[str, int], ...]
    ontology_iris: tuple[str, ...]
    languages: tuple[str, ...]
