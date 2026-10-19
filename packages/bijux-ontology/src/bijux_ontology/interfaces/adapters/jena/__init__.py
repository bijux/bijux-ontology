"""Explicit local Apache Jena adapters, optional to ordinary core imports."""

from .runtime import prepare_jena_backend
from .types import JenaBackend

__all__ = ["JenaBackend", "prepare_jena_backend"]
