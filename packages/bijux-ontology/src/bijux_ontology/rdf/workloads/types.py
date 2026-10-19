"""Finite query corpora and explicit native engine configurations."""

from dataclasses import dataclass
from typing import Literal as Choice

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.queries.native import types as native_types
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata

QualificationStatus = Choice[
    "agreeing", "disagreeing", "unsupported", "failed", "exhausted"
]


@dataclass(frozen=True)
class WorkloadQuery:
    """One supplied query qualified under explicit bag or set answer semantics."""

    identifier: str
    text: str
    comparison: Choice["bag", "set"] = "bag"

    def __post_init__(self) -> None:
        """Reject implicit identifiers, empty queries and order claims."""
        if (
            type(self.identifier) is not str
            or not self.identifier
            or type(self.text) is not str
            or not self.text.strip()
        ):
            raise InputError("Workload queries require nonempty identifiers and SPARQL")
        if self.comparison not in ("bag", "set"):
            raise InputError(
                "Workload qualification supports explicit bag or set semantics"
            )


@dataclass(frozen=True)
class QueryQualification:
    """Both native observations and exact finite answer comparison policy."""

    query: WorkloadQuery
    engines: tuple[native_types.EngineAnswer, ...]
    status: QualificationStatus
    report: Report


@dataclass(frozen=True)
class WorkloadQualification:
    """Scoped finite observations, without universal engine or ordering claims."""

    scope: QueryScope
    source_metadata: SnapshotMetadata | None
    queries: tuple[QueryQualification, ...]
    report: Report
