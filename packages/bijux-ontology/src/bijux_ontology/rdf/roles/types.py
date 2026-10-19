"""Logical roles attach to exact term positions, never to an entire statement."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import BNode, URIRef
from rdflib.term import Node

TermRole = Choice[
    "class",
    "concept",
    "individual",
    "property",
    "object_property",
    "datatype_property",
    "annotation_property",
    "datatype",
    "structural",
    "declaration",
    "unknown",
]
TermPosition = Choice["subject", "predicate", "object", "datatype", "graph"]
Statement = tuple[Node, Node, Node]


@dataclass(frozen=True)
class TermRoleOccurrence:
    """One qualified axiom position with original terms and selected ownership.

    Blank-node and graph identifiers belong to the supplying owned snapshot.
    A marker/unknown occurrence cannot discharge an application individual use.
    """

    term: Node
    position: TermPosition
    role: TermRole
    statement: Statement
    source_scope: str = ""
    graph: URIRef | BNode | None = None
