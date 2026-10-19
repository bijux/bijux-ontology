"""Typed finite source traversals and asserted entity selections."""

from .checking import resolve_selection
from .types import (
    ArrayItems,
    CoverageUnit,
    EntitySelector,
    KeyedItems,
    Property,
    Selection,
    SourceSelector,
    Step,
)

__all__ = [
    "ArrayItems",
    "CoverageUnit",
    "EntitySelector",
    "KeyedItems",
    "Property",
    "Selection",
    "SourceSelector",
    "Step",
    "resolve_selection",
]
