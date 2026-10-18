"""Common-base RDF merge plans with explicit structural conflict resolution."""

from .checking import apply_rdf_merge
from .planning import plan_rdf_merge
from .types import (
    MergeChanges,
    MergeConflict,
    MergeCorrespondence,
    MergeResolution,
    RdfMergePlan,
    RdfMergeResult,
)

__all__ = [
    "MergeChanges",
    "MergeConflict",
    "MergeCorrespondence",
    "MergeResolution",
    "RdfMergePlan",
    "RdfMergeResult",
    "apply_rdf_merge",
    "plan_rdf_merge",
]
