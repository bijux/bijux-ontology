"""Bounded structured JSON values remain opaque original RDF literals."""

from .checking import check_json_literal
from .types import JSONKind, JsonLiteralCheck, JsonLiteralContract, JsonRequirement

__all__ = [
    "JSONKind",
    "JsonLiteralCheck",
    "JsonLiteralContract",
    "JsonRequirement",
    "check_json_literal",
]
