"""Offline OBOGraphs class axioms and qualified metadata with explicit loss scope."""

from .reading import read_obo_graphs
from .types import OboGraphsDocument, OboGraphsExport
from .writing import write_obo_graphs

__all__ = [
    "OboGraphsDocument",
    "OboGraphsExport",
    "read_obo_graphs",
    "write_obo_graphs",
]
