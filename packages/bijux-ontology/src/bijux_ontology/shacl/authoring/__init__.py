"""Typed finite SHACL Core construction with explicit references and RDF export."""

from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.parameters import ShaclParameter
from bijux_ontology.shacl.authoring.types import (
    ShaclComparison,
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclQualifiedCount,
    ShaclShapeRef,
    ShaclValue,
)

__all__ = [
    "ShaclParameter",
    "ShaclComparison",
    "ShaclConstraint",
    "ShaclCount",
    "ShaclLogical",
    "ShaclNodeShape",
    "ShaclPropertyShape",
    "ShaclQualifiedCount",
    "ShaclShapeRef",
    "ShaclValue",
    "compile_shapes",
]
