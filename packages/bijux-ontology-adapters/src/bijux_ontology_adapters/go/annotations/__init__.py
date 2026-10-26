"""Explicit current GO annotation formats and signed, loss-aware interchange."""

from .checking import check_go_annotations
from .reading import read_go_annotations
from .types import (
    GoAnnotation,
    GoAnnotationDocument,
    GoAnnotationExport,
    GoAnnotationInventory,
    GoAnnotationPolicy,
    GoAnnotations,
    GoEvidence,
    GoExtension,
    GoGeneProduct,
    GoGeneProductDocument,
    GoReference,
)
from .writing import annotations_to_rdf, write_go_annotations

__all__ = [
    "GoAnnotation",
    "GoAnnotationDocument",
    "GoAnnotationExport",
    "GoAnnotationInventory",
    "GoAnnotationPolicy",
    "GoAnnotations",
    "GoEvidence",
    "GoExtension",
    "GoGeneProduct",
    "GoGeneProductDocument",
    "GoReference",
    "annotations_to_rdf",
    "check_go_annotations",
    "read_go_annotations",
    "write_go_annotations",
]
