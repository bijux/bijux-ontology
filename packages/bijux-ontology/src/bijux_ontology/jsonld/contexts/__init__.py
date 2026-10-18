"""Finite maintained-processor context views and effective-definition differences."""

from .comparison import compare_context_definitions
from .processing import inspect_context
from .types import (
    ContextChange,
    ContextComparison,
    ContextDefaults,
    ContextInspection,
    ContextScope,
    ContextView,
    TermDefinition,
)

__all__ = [
    "ContextChange",
    "ContextComparison",
    "ContextDefaults",
    "ContextInspection",
    "ContextScope",
    "ContextView",
    "TermDefinition",
    "compare_context_definitions",
    "inspect_context",
]
