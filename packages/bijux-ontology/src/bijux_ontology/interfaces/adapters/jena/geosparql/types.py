"""Explicit geometry values and independently qualified spatial obligations."""

from dataclasses import dataclass
from typing import Literal

from rdflib import Literal as RdfLiteral

from bijux_ontology.diagnostics.reports import Report


@dataclass(frozen=True)
class GeometryValue:
    """Bind one exact supplied RDF literal to a stable caller key and location."""

    key: str
    literal: RdfLiteral
    location: str = ""


@dataclass(frozen=True)
class SpatialRelation:
    """Require an explicitly selected native topological relationship."""

    left: str
    right: str
    relation: Literal[
        "within", "contains", "intersects", "disjoint", "topological_equals"
    ]
    expected: bool = True
    location: str = ""


@dataclass(frozen=True)
class SpatialDistance:
    """Bound native planar distance in the explicit unit, without geodesic claims."""

    left: str
    right: str
    maximum: float
    unit: str
    location: str = ""


@dataclass(frozen=True)
class GeometryOutcome:
    """Preserve source spelling separately from lexical, geometry and CRS evidence."""

    key: str
    lexical: str
    datatype: str | None
    language: str | None
    location: str
    lexical_valid: bool | None
    geometry_valid: bool | None
    crs: str | None
    axis_order: str | None
    dimensions: int | None
    empty: bool | None
    report: Report

    @property
    def literal(self) -> RdfLiteral:
        """Return an independent literal retaining exact original text and metadata."""
        return RdfLiteral(
            self.lexical, lang=self.language, datatype=self.datatype, normalize=False
        )


@dataclass(frozen=True)
class SpatialOutcome:
    """Keep actual native value and exact RDF identity distinct from acceptance."""

    request: SpatialRelation | SpatialDistance
    value: bool | float | None
    rdf_terms_equal: bool
    report: Report


@dataclass(frozen=True)
class GeometryCheck:
    """Describe only supplied values and obligations under the finite native profile."""

    geometries: tuple[GeometryOutcome, ...]
    relations: tuple[SpatialOutcome, ...]
    distances: tuple[SpatialOutcome, ...]
    backend: str | None
    report: Report
