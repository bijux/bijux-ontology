"""Owned source selections retain graph provenance beside existing union readers."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, Graph, URIRef

from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource

GraphKind = Literal["graph", "default", "named"]


def source_role(scope: str) -> str:
    """Source array order is not semantic ownership; local/import/lookup roles are."""
    return (
        "imports"
        if scope.startswith("import[")
        else "lookup"
        if scope.startswith("lookup[")
        else scope
    )


@dataclass(frozen=True)
class OwnedSource:
    """Reuse one prepared resource and its exact selected semantic graph."""

    prepared: PreparedSource
    selected: Graph
    graph_name: GraphSelection | None = None
    union: bool = False

    def contexts(
        self, budget: Budget
    ) -> Iterator[tuple[GraphKind, URIRef | BNode | None, Graph]]:
        """Expose original selected contexts without copying or guessing names."""
        yield from self.prepared._contexts(
            self.graph_name, union=self.union, budget=budget
        )
