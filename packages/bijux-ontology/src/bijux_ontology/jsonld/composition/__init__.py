"""Explicit ordered context composition with retained representative contracts."""

from .checking import compose_contexts
from .types import ContextCase, ContextCaseResult, ContextComposition, ContextModule

__all__ = [
    "ContextCase",
    "ContextCaseResult",
    "ContextComposition",
    "ContextModule",
    "compose_contexts",
]
