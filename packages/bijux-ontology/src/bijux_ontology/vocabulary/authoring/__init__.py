"""Checked native SKOS and ontology annotation authoring."""

from .checking import author_vocabulary
from .types import (
    AnnotationChange,
    CreateSkosResource,
    DeprecateResource,
    PreferredLabelChange,
    QualifiedAnnotationChange,
    SchemeMembership,
)

__all__ = [
    "AnnotationChange",
    "CreateSkosResource",
    "DeprecateResource",
    "PreferredLabelChange",
    "QualifiedAnnotationChange",
    "SchemeMembership",
    "author_vocabulary",
]
