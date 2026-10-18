"""Explicit immutable CURIE registries and ownership-aware atomic composition."""

from .merging import RegistryMerge, merge_identifier_registry
from .types import IdentifierRegistry, NamespaceContribution

__all__ = [
    "IdentifierRegistry",
    "NamespaceContribution",
    "RegistryMerge",
    "merge_identifier_registry",
]
