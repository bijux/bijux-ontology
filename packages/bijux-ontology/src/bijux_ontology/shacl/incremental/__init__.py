"""Explicit checked RDF deltas and conservative maintained SHACL revalidation."""

from .checking import revalidate
from .preparation import prepare_incremental
from .types import IncrementalPreparation, IncrementalResult, IncrementalState

__all__ = [
    "IncrementalPreparation",
    "IncrementalResult",
    "IncrementalState",
    "prepare_incremental",
    "revalidate",
]
