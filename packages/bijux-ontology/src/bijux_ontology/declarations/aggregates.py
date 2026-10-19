"""Explicit arithmetic and population policies for related record groups."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef
from rdflib.paths import Path

from bijux_ontology.rdf.paths.types import RdfPath


@dataclass(frozen=True)
class AggregateContract:
    """A selected finite aggregate, without application-specific totals.

    count counts bound records; distinct_count counts exact RDF member values.
    Numeric functions accept validated XSD integer/decimal values only. unit=None
    explicitly declares dimensionless arithmetic; otherwise unit_path must
    establish the exact selected unit for every participating record. No unit
    conversion, inferred partitions or implicit population completeness is used.
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
