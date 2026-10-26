"""Explicit offline JSON Schema 2020-12 RDF output contracts."""

from .checking import check_instance
from .extraction import model_declarations
from .types import SchemaModel

__all__ = ["SchemaModel", "check_instance", "model_declarations"]
