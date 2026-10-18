"""Explicit caller-scoped SKOS-XL relation and endpoint contracts."""

from .checking import check_xl_relations
from .types import XLRelation, XLRelationPolicy

__all__ = ["XLRelation", "XLRelationPolicy", "check_xl_relations"]
