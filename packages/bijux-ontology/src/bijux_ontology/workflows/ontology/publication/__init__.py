"""Dependency-complete finite SKOS publication workflows."""

from .projection import compile_skos_publication
from .types import SkosPublication

__all__ = ("SkosPublication", "compile_skos_publication")
