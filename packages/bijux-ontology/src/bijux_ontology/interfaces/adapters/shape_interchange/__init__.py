"""Declared nonrecursive SHACL/ShEx fragment with actual native comparisons."""

from .checking import check_interchange
from .compilation import shacl_to_shex, shex_to_shacl
from .types import (
    InterchangeCheck,
    InterchangeObligation,
    InterchangeRemainder,
    ShapePreservation,
    ShapeTranslation,
)

__all__ = [
    "InterchangeCheck",
    "InterchangeObligation",
    "InterchangeRemainder",
    "ShapePreservation",
    "ShapeTranslation",
    "check_interchange",
    "shacl_to_shex",
    "shex_to_shacl",
]
