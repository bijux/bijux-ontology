"""Explicit finite entailment preparation and its ordinary RDF projection."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource

EntailmentRegime: TypeAlias = Literal["rdfs", "owl-rl"]
GeneralizedTriple: TypeAlias = tuple[QueryTerm, QueryTerm, QueryTerm]


@dataclass(frozen=True)
class EntailmentResult:
    """Separate asserted data, ordinary consequences and final generalized facts.

    A verified report proves completed selected maintained rule closure and exact
    retention of its ordinary RDF projection and generalized witnesses. It is
    not OWL 2 DL consistency, profile certification, or an emitted-field check.
    Returned snapshots and generalized terms share one owned blank identity
    capture; their blanks never establish identity with another input document.
    A requested union is explicitly flattened, rather than assigned back to any
    physical graph. Ordinary pinned selections retain their actual graph scope.
    """

    asserted: PreparedSource | None
    derived: PreparedSource | None
    effective: PreparedSource | None
    generalized: tuple[GeneralizedTriple, ...]
    report: Report
    regime: EntailmentRegime
    backend: str = ""
    projection: Literal["ordinary-rdf"] = "ordinary-rdf"
    graph_name: GraphSelection | None = None
    union: bool = False
