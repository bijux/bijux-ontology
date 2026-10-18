"""Required qualification beside an owned logical field shape artifact."""

from dataclasses import dataclass

from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class PathFieldProjection:
    """A supported Core projection; its report remains required for coverage."""

    report: Report
    source: PreparedSource | None = None
