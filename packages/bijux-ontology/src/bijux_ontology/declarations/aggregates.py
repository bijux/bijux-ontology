"""Explicit arithmetic and population policies for related record groups."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef
from rdflib.paths import Path

from bijux_ontology.rdf.paths.types import RdfPath


@dataclass(frozen=True)
class RdfUnitBinding:
    """Bind an actual unit IRI to one explicitly selected native atomic unit."""

    unit: URIRef
    pint_unit: str


@dataclass(frozen=True)
class AggregateUnitConversion:
    """Select exact multiplicative length/time conversion to the aggregate unit."""

    bindings: tuple[RdfUnitBinding, ...]
    profile: Literal["multiplicative-length-time"] = "multiplicative-length-time"


@dataclass(frozen=True)
class AggregateContract:
    """A selected finite aggregate, without application-specific totals.

    count counts bound records; distinct_count counts exact RDF member values.
    Numeric functions accept validated XSD integer/decimal values only. unit=None
    explicitly declares dimensionless arithmetic; otherwise unit_path must
    establish the selected unit for every participating record. Partitions and
    population completeness are never inferred. Conversion
    requires an explicit finite multiplicative length/time unit contract.
    """

    name: str
    function: Literal["count", "distinct_count", "sum", "min", "max"]
    counting: Literal["entities", "occurrences"]
    member_path: URIRef | Path | RdfPath | None = None
    comparison: Literal["equal", "at_most", "at_least"] = "equal"
    missing: Literal["reject", "exclude", "unverified"] = "unverified"
    multiplicity: Literal["reject", "unverified"] = "unverified"
    empty: Literal["zero", "unverified"] = "unverified"
    unit: URIRef | None = None
    unit_path: URIRef | Path | RdfPath | None = None
    conversion: AggregateUnitConversion | None = None
