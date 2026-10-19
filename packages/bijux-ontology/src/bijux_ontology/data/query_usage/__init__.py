"""Bounded static vocabulary dependencies from maintained SPARQL parsing."""

from .checking import check_query_usage
from .reading import read_query_usage
from .types import QueryDynamicScope, QueryReference, QueryRole, QueryUsage

__all__ = [
    "QueryDynamicScope",
    "QueryReference",
    "QueryRole",
    "QueryUsage",
    "check_query_usage",
    "read_query_usage",
]
