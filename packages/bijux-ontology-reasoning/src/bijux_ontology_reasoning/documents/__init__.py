"""Original native ontology capture and explicit loss-qualified conversion."""

from .conversion import convert_ontology
from .preparation import prepare_owl_document
from .types import (
    ConversionChange,
    NativeExport,
    NativeOntology,
    NativeProfile,
    OwlDocument,
    OwlSyntax,
)

__all__ = [
    "ConversionChange",
    "NativeExport",
    "NativeOntology",
    "NativeProfile",
    "OwlDocument",
    "OwlSyntax",
    "convert_ontology",
    "prepare_owl_document",
]
