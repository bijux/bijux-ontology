"""Qualified immutable native input and exact single-run execution evidence."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.answers.types import AnswerArtifact
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata

EngineStatus = Choice["completed", "unsupported", "failed", "exhausted"]
GraphRecord = tuple[
    QueryTerm | None, tuple[tuple[QueryTerm, QueryTerm, QueryTerm], ...]
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


@dataclass(frozen=True, init=False, slots=True)
class NativeQueryInput:
    """One paid physical selection, exact namespaces and finite native bindings.

    Graph records retain empty graph inventory and original immutable RDF nodes.
    The executed scope belongs to this captured input, not to its future caller.
    """

    scope: QueryScope
    source_metadata: SnapshotMetadata
    graphs: tuple[GraphRecord, ...]
    namespaces: tuple[tuple[str, str], ...]
    bindings: tuple[QueryBinding, ...]

    def __init__(self) -> None:
        """Require bounded selected-input capture rather than caller-shaped records."""
        raise InputError("Use capture_selected_input to construct native query input")

    @classmethod
    def _new(
        cls,
        scope: QueryScope,
        metadata: SnapshotMetadata,
        graphs: tuple[GraphRecord, ...],
        namespaces: tuple[tuple[str, str], ...],
        bindings: tuple[QueryBinding, ...],
    ) -> "NativeQueryInput":
        """Publish only after complete bounded selection and binding qualification."""
        value = object.__new__(cls)
        object.__setattr__(value, "scope", scope)
        object.__setattr__(value, "source_metadata", metadata)
        object.__setattr__(value, "graphs", graphs)
        object.__setattr__(value, "namespaces", namespaces)
        object.__setattr__(value, "bindings", bindings)
        return value
