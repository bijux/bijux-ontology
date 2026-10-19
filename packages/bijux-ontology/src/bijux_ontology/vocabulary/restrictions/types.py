"""Original axiom witnesses for finite restriction projections and paths."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef
from rdflib.term import Node

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.browsing.results import Edge


@dataclass(frozen=True)
class RestrictionProjection:
    """One supported restriction or retained uninterpreted expression alternative.

    ``owner`` is the requested class, while ``authored_owner`` and ``axiom``
    identify the actual attachment. ``inheritance`` contains original named
    subclass and intersection/list evidence; ``structure`` retains the
    expression and list assertions.
    These records never assert an RDF object-property triple.
    """

    owner: URIRef
    authored_owner: URIRef
    axiom: Edge
    expression: Node
    property: URIRef | None
    quantifier: URIRef | None
    filler: Node | None
    status: Literal["supported", "malformed", "unsupported"]
    inheritance: tuple[Edge, ...]
    structure: tuple[Edge, ...]


@dataclass(frozen=True)
class RestrictionPath:
    """Nonempty existential connection and its separate projection coverage truth."""

    status: Literal["found", "unreachable", "incomplete"]
    steps: tuple[RestrictionProjection, ...]
    report: Report
