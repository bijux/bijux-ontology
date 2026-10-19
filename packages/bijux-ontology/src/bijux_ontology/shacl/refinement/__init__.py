"""Finite explicitly scoped SHACL profile implication and weakening witnesses."""

from .checking import compare_shape_refinement
from .types import ProfileRefinement, RefinementContract, RefinementProof

__all__ = [
    "ProfileRefinement",
    "RefinementContract",
    "RefinementProof",
    "compare_shape_refinement",
]
