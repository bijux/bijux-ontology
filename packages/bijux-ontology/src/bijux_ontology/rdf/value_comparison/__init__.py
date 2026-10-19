"""Explicit literal value comparison without changing RDF term equality."""

from .comparing import compare_literal_values
from .types import LiteralValueComparison

__all__ = ["LiteralValueComparison", "compare_literal_values"]
