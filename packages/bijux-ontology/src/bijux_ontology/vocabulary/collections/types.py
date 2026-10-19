"""Owned nested-collection occurrences and explicit ordering boundaries."""

from dataclasses import dataclass
from typing import Literal

from rdflib.term import Node

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.browsing.navigation import Membership


@dataclass(frozen=True)
class CollectionOccurrence:
    """One terminal member and its actual inclusion assertions/list positions.

    Anonymous terms belong to this owned vocabulary snapshot. Paths with shared
    children remain distinct; their storage order does not establish RDF order.
    """

    member: Node
    kind: Literal["concept", "collection", "unresolved"]
    path: tuple[Membership, ...]


@dataclass(frozen=True)
class CollectionExpansion:
    """Selected concept set and retained paths, with total order only when proved.

    Retain mode exposes nested collections atomically; their descendant concepts
    are excluded. ``sequence`` includes duplicates only when every selected
    branch supplies complete list order, with no unordered contribution.
    """

    collection: Node
    nested: Literal["retain", "expand"]
    members: tuple[CollectionOccurrence, ...]
    concepts: frozenset[Node]
    sequence: tuple[Node, ...] | None
    ordering: Literal["total", "unordered", "mixed", "incomplete"]
    report: Report
