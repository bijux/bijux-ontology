"""Finite actual application/context/ontology deployment evidence."""

from .checking import check_compatibility_matrix
from .types import (
    CellCompatibility,
    CompatibilityMatrix,
    ConsumerCompatibility,
    ConsumerVersion,
    MatrixCell,
    MatrixContext,
)

__all__ = [
    "CellCompatibility",
    "CompatibilityMatrix",
    "ConsumerCompatibility",
    "ConsumerVersion",
    "MatrixCell",
    "MatrixContext",
    "check_compatibility_matrix",
]
