"""Actual assertion endpoints grouped only by established individual identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rdflib import BNode, URIRef

from .types import TermKey

if TYPE_CHECKING:
    from .context import AssertionContext, _Row


class PropertyRows:
    """Retain original row order and premises beside finite identity buckets."""

    def __init__(self, state: AssertionContext, predicate: URIRef) -> None:
        self.subjects: dict[TermKey, list[_Row]] = {}
        self.edges: dict[tuple[TermKey, TermKey], list[_Row]] = {}
        self.positions: dict[int, int] = {}
        for position, row in enumerate(state.rows(predicate)):
            state.operation.consume("work")
            state.operation.consume("states", 3)
            state.operation.reserve_storage(256)
            subject = state.identity.find(state.key(row.subject))
            self.subjects.setdefault(subject, []).append(row)
            self.positions[id(row)] = position
            if isinstance(row.value, URIRef | BNode):
                value = state.identity.find(state.key(row.value))
                self.edges.setdefault((subject, value), []).append(row)
