"""Explicit named OWL taxonomy projection to checked SKOS publications."""

from .projection import project_taxonomy
from .types import (
    ClassConcept,
    PreferredClassLabel,
    TaxonomyExclusion,
    TaxonomyProjection,
    TaxonomyPublication,
)

__all__ = [
    "ClassConcept",
    "PreferredClassLabel",
    "TaxonomyExclusion",
    "TaxonomyProjection",
    "TaxonomyPublication",
    "project_taxonomy",
]
