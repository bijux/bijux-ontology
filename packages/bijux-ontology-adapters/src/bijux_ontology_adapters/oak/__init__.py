"""Optional concrete OAK access without an import-time native dependency."""

from .preparation import as_oak
from .types import OakOperationError, OakPreparation

__all__ = ["OakOperationError", "OakPreparation", "as_oak"]
