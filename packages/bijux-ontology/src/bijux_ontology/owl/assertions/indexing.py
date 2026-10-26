"""Actual assertion endpoints grouped only by established individual identity."""

from __future__ import annotations

from collections.abc import Callable

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.execution.budgets import Operation

from .identity import Identity
from .types import PropertyAssertion, TermKey


class PropertyRows:
    """Retain original row order and premises beside finite identity buckets."""

    def __init__(
        self,
        rows: tuple[PropertyAssertion, ...],
        identity: Identity,
        operation: Operation,
        key: Callable[[Node], TermKey],
    ) -> None:
        self.subjects: dict[TermKey, list[PropertyAssertion]] = {}
        self.edges: dict[tuple[TermKey, TermKey], list[PropertyAssertion]] = {}
        self.positions: dict[int, int] = {}
        for position, row in enumerate(rows):
            operation.consume("work")
            operation.consume("states", 3)
            operation.reserve_storage(256)
            subject = identity.find(key(row.subject))
            self.subjects.setdefault(subject, []).append(row)
            self.positions[id(row)] = position
            if isinstance(row.value, URIRef | BNode):
                value = identity.find(key(row.value))
                self.edges.setdefault((subject, value), []).append(row)
