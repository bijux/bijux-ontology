"""Explicit owned DL consistency and named-class satisfiability checks."""

from ..inference.consistency.checking import check_consistency
from ..inference.consistency.types import (
    ClassSatisfiability,
    ConsistencyCheck,
    ReasonerIdentity,
)

__all__ = [
    "ClassSatisfiability",
    "ConsistencyCheck",
    "ReasonerIdentity",
    "check_consistency",
]
