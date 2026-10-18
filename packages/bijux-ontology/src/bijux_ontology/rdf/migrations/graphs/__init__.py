"""Explicit dataset repartitioning and intentional partition merges."""

from bijux_ontology.rdf.migrations.graphs.checking import migrate_graphs
from bijux_ontology.rdf.migrations.graphs.types import GraphRoute

__all__ = ["GraphRoute", "migrate_graphs"]
