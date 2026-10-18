"""Finite used-identifier release comparison and explicit advisory updates."""

from .comparison import compare_identifier_releases
from .types import (
    IdentifierEquivalence,
    IdentifierImpact,
    IdentifierRelease,
    IdentifierReleaseImpact,
    IdentifierUpdate,
)

__all__ = [
    "IdentifierEquivalence",
    "IdentifierImpact",
    "IdentifierRelease",
    "IdentifierReleaseImpact",
    "IdentifierUpdate",
    "compare_identifier_releases",
]
