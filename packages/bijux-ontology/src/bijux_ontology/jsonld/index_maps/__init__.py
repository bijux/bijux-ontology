"""Document-semantic JSON-LD map-key and processed index contracts."""

from .checking import check_index_maps
from .types import IndexBinding, IndexMapCheck, IndexMapExpectation, IndexMapObservation

__all__ = [
    "IndexBinding",
    "IndexMapCheck",
    "IndexMapExpectation",
    "IndexMapObservation",
    "check_index_maps",
]
