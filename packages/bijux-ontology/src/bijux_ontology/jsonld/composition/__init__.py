"""Explicit ordered context composition with retained representative contracts."""

from bijux_ontology.jsonld.contracts.cases import ContextCase

from .checking import compose_contexts
from .types import ContextCaseResult, ContextComposition, ContextModule

__all__ = [
    "ContextCase",
    "ContextCaseResult",
    "ContextComposition",
    "ContextModule",
    "compose_contexts",
]
