"""Finite endpoint selections and explicit server completeness obligations."""

from dataclasses import dataclass

from rdflib import Literal, URIRef

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class EndpointQuery:
    """A parameterized distinct triple selection in one explicit graph scope.

    None selects the endpoint's default graph, never an implicit union. Terms
    bind through VALUES; this profile does not accept executable query fragments.
    """

    subject: URIRef | None = None
    predicate: URIRef | None = None
    object: URIRef | Literal | None = None
    graph: URIRef | None = None


@dataclass(frozen=True)
class EndpointCompleteness:
    """A declared immutable-version protocol, confirmed on every result page.

    The endpoint must guarantee that snapshot_parameter selects the immutable
    dataset identified by snapshot. Headers confirm that same version, the exact
    distinct row total for this query, and absence of truncation (literal false).
    These are caller-declared server guarantees, not inferred HTTP properties.
    """

    snapshot_parameter: str
    snapshot: str
    snapshot_header: str
    total_header: str
    truncated_header: str


@dataclass(frozen=True)
class EndpointPage:
    """Bounded response identity and the exact requested row window."""

    url: str
    sha256: str
    offset: int
    rows: int
    snapshot: str | None
    total: int | None


@dataclass(frozen=True)
class EndpointExtraction:
    """An owned offline slice beside independently qualified acquisition truth."""

    source: PreparedSource | None
    query: EndpointQuery | None
    pages: tuple[EndpointPage, ...]
    rows: int
    complete: bool
    consistent: bool
    report: Report
