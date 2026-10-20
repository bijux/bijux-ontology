"""Required qualification beside an owned logical field shape artifact."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class PathFieldProjection:
    """A supported Core projection; its report remains required for coverage."""

    report: Report
    source: PreparedSource | None = None
