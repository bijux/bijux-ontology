"""Explicit maintained syntactic-locality modules with checked consumer proofs."""

from ..extraction.locality.checking import extract_locality_module
from ..extraction.locality.types import LocalityAxiom, LocalityMethod, LocalityModule

__all__ = [
    "LocalityAxiom",
    "LocalityMethod",
    "LocalityModule",
    "extract_locality_module",
]
