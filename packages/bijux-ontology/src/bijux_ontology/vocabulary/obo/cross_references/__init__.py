"""Curated xrefs preserve attachment, accession validity and local scope separately."""

from .checking import check_cross_references
from .types import (
    CrossReferenceAssertion,
    CrossReferenceCheck,
    CrossReferenceObservation,
    LocalCatalogue,
)

__all__ = [
    "CrossReferenceAssertion",
    "CrossReferenceCheck",
    "CrossReferenceObservation",
    "LocalCatalogue",
    "check_cross_references",
]
