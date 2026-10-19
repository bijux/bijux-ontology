"""Finite term-position roles, independent of vocabulary or application checks."""

from .classification import classify_statement
from .types import TermRoleOccurrence

__all__ = ["TermRoleOccurrence", "classify_statement"]
