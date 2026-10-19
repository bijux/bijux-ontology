"""Nested SKOS collection paths with explicit ordering boundaries."""

from .expansion import expand_collection
from .types import CollectionExpansion, CollectionOccurrence

__all__ = ["CollectionExpansion", "CollectionOccurrence", "expand_collection"]
