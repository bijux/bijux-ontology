"""Finite query corpora and explicit native engine configurations."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.answers.types import AnswerArtifact
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata

EngineStatus = Choice["completed", "unsupported", "failed", "exhausted"]
QualificationStatus = Choice[
    "agreeing", "disagreeing", "unsupported", "failed", "exhausted"
]


@dataclass(frozen=True)
class EngineConfig:
    """Native default-graph and asserted-entailment settings, never hidden defaults."""

    engine: Choice["rdflib", "pyoxigraph"]
    default_graph: Choice["authored", "union"] = "authored"
    entailment: str = "asserted"

    def __post_init__(self) -> None:
        """Require concrete maintained adapters and typed settings."""
        if self.engine not in ("rdflib", "pyoxigraph"):
            raise InputError("Choose the RDFLib or Pyoxigraph workload adapter")
        if (
            self.default_graph not in ("authored", "union")
            or type(self.entailment) is not str
        ):
            raise InputError("Choose explicit workload graph and entailment settings")


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
class QueryBinding:
    """Exact named-variable input for the two declared native binding APIs."""

    variable: str
    value: URIRef | Literal

    def __post_init__(self) -> None:
        """Exclude ambiguous foreign blank identities and callable extensions."""
        if (
            type(self.variable) is not str
            or not self.variable
            or not isinstance(self.value, URIRef | Literal)
        ):
            raise InputError("Workload bindings require names and IRI/literal terms")


@dataclass(frozen=True)
class EngineAnswer:
    """Exact runtime identity and a fully captured answer, when actually completed."""

    config: EngineConfig
    version: str | None
    python_version: str | None
    binding_policy: str
    status: EngineStatus
    answer: AnswerArtifact | None
    report: Report


@dataclass(frozen=True)
class QueryQualification:
    """Both native observations and exact finite answer comparison policy."""

    query: WorkloadQuery
    engines: tuple[EngineAnswer, ...]
    status: QualificationStatus
    report: Report


@dataclass(frozen=True)
class WorkloadQualification:
    """Scoped finite observations, without universal engine or ordering claims."""

    scope: QueryScope
    source_metadata: SnapshotMetadata | None
    queries: tuple[QueryQualification, ...]
    report: Report
