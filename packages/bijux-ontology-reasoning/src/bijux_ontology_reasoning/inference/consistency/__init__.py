"""Explicit owned DL consistency and named-class satisfiability checks."""

from .checking import check_consistency
from .types import ClassSatisfiability, ConsistencyCheck

__all__ = [
    "ClassSatisfiability",
    "ConsistencyCheck",
    "check_consistency",
]
