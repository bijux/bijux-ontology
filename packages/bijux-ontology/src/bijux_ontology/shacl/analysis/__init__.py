"""Finite static incompatibility and recursion boundaries for selected SHACL."""

from bijux_ontology.shacl.analysis.checking import analyze_shape_profile
from bijux_ontology.shacl.analysis.types import (
    LocalConflict,
    RootConflict,
    ShapeAnalysis,
    ShapeComponent,
    SignedDependency,
)

__all__ = [
    "LocalConflict",
    "RootConflict",
    "ShapeAnalysis",
    "ShapeComponent",
    "SignedDependency",
    "analyze_shape_profile",
]
