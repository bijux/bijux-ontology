"""Explicit query scopes and exact immutable completed answer terms."""

from dataclasses import dataclass
from typing import Literal as Kind

from rdflib import BNode, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.sources.snapshots import GraphSelection


@dataclass(frozen=True)
class QueryScope:
    """A query dataset or a graph projection used as its sole default graph.

    Dataset preserves authored default/named graphs. Default, named and union
    project only selected triples into a graph; GRAPH cannot escape that choice.
    Named blanks must be exact identifiers from a PreparedSource dataset export.
    """

    kind: Kind["default", "named", "dataset", "union"]
    graph_name: GraphSelection | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "default",
            "named",
            "dataset",
            "union",
        }:
            raise InputError("Query scope must be default, named, dataset or union")
        if self.kind == "named":
            if self.graph_name is None:
                raise InputError("Named query scope requires an authored graph name")
            if not isinstance(self.graph_name, BNode):
                name = absolute_iri(str(self.graph_name))
                if name == str(DATASET_DEFAULT_GRAPH_ID):
                    raise InputError("Use default scope for the RDF default graph")
                object.__setattr__(self, "graph_name", URIRef(name))
        elif self.graph_name is not None:
            raise InputError("Only named query scope accepts a graph name")


@dataclass(frozen=True)
class QueryResult:
    """Completed query truth beside raw answers and exact immutable RDF terms.

    A false ASK is a completed answer, not a diagnostic disagreement. SELECT
    row positions align with variables; None is unbound. Blank terms are local
    to this result and never imply identity with another query or input export.
    """

    report: Report
    scope: QueryScope
    kind: Kind["ASK", "SELECT"] | None = None
    answer: bool | None = None
    variables: tuple[str, ...] = ()
    rows: tuple[tuple[QueryTerm | None, ...], ...] = ()
