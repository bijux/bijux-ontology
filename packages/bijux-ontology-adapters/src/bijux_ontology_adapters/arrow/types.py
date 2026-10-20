"""Explicit column semantics and owned results over maintained Arrow objects."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class ArrowColumn:
    """One actual physical column mapped by consumer policy, never by its name."""

    field: str
    column: str
    predicate: URIRef
    kind: Choice["iri", "literal"]
    datatype: URIRef | None = None
    list: bool = False
    null: Choice["omit", "reject"] = "omit"
    nan: Choice["preserve", "reject"] = "reject"
    timezone: str | None = None


@dataclass(frozen=True, init=False)
class ArrowContract:
    """Factory-owned explicit mapping over an immutable compiled model snapshot."""

    models: PreparedModels
    model: str
    subject_column: str
    columns: tuple[ArrowColumn, ...]
    types: tuple[URIRef, ...]
    graph_name: URIRef | str
    ignored_columns: tuple[str, ...]

    def __new__(cls) -> "ArrowContract":
        """Require preparation instead of adopting unqualified mapping fields."""
        raise TypeError("Use prepare_arrow_contract")


@dataclass(frozen=True)
class ArrowCell:
    """Actual source scalar/type, null/NaN state and exact mapped RDF endpoints."""

    column: str
    field: str
    scalar: object
    source_type: object
    null: bool
    nan: bool
    terms: tuple[Node, ...]


@dataclass(frozen=True)
class ArrowRowResult:
    """One original batch/row identity and its retained semantic child report."""

    index: int
    subject: URIRef | None
    cells: tuple[ArrowCell, ...]
    report: Report


@dataclass(frozen=True)
class ArrowBatchResult:
    """One bounded native batch with optional consumer-requested owned RDF."""

    index: int
    rows: tuple[ArrowRowResult, ...]
    report: Report
    output: PreparedSource | None = None
