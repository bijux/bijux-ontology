"""Explicit controlled-set algebra and captured record-dependent bindings."""

from .algebra import compile_allowed
from .selection import compile_bindings, select_binding
from .types import (
    AllowedDifference,
    AllowedExpression,
    AllowedIntersection,
    AllowedUnion,
    BindingCase,
    BindingSelection,
    CompiledBindingCase,
    CompiledBindings,
)

__all__ = [
    "AllowedDifference",
    "AllowedExpression",
    "AllowedIntersection",
    "AllowedUnion",
    "BindingCase",
    "BindingSelection",
    "CompiledBindingCase",
    "CompiledBindings",
    "compile_allowed",
    "compile_bindings",
    "select_binding",
]
