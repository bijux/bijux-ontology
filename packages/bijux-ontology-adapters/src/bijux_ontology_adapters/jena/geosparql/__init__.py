"""Optional bounded local Jena GeoSPARQL1.0 geometry and relation contracts."""

from .checking import check_geometries
from .types import (
    GeometryCheck,
    GeometryOutcome,
    GeometryValue,
    SpatialDistance,
    SpatialOutcome,
    SpatialRelation,
)

__all__ = [
    "GeometryCheck",
    "GeometryOutcome",
    "GeometryValue",
    "SpatialDistance",
    "SpatialOutcome",
    "SpatialRelation",
    "check_geometries",
]
