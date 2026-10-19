"""Opt-in bounded XML canonical values and honest HTML fragment inspection."""

from .checking import compare_markup_values, inspect_markup_literal
from .types import (
    MarkupCanonicalization,
    MarkupComparison,
    MarkupInspection,
    MarkupRelation,
)

__all__ = [
    "MarkupCanonicalization",
    "MarkupComparison",
    "MarkupInspection",
    "MarkupRelation",
    "compare_markup_values",
    "inspect_markup_literal",
]
