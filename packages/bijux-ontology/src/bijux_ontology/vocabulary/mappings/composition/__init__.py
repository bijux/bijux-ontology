"""Explicit relation-preserving mapping composition and original path evidence."""

from .checking import compose_mappings
from .types import (
    ComposedMapping,
    MappingComposition,
    MappingCompositionPath,
    MappingCompositionRule,
    MappingOccurrence,
)

__all__ = [
    "ComposedMapping",
    "MappingComposition",
    "MappingCompositionPath",
    "MappingCompositionRule",
    "MappingOccurrence",
    "compose_mappings",
]
