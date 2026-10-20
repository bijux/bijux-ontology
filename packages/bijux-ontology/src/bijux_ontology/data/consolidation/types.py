"""Owned corpus contributions and explicit application agreement policies."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.types import PatchQuad, PatchResource, SnapshotWitness
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node


@dataclass(frozen=True)
class CorpusDocument:
    """Explicit participation of an already prepared document and physical graphs.

    None uses the source's authorized scope. An explicit immutable graph tuple
    narrows that scope; None inside the tuple means its default graph. Independent
    document entries always keep separate blank identities. Base resolution has
    already occurred in preparation; origin metadata retains both supplied bases.
    """

    identifier: str
    source: PreparedSource
    graphs: tuple[URIRef | BNode | None, ...] | None = None

    def __post_init__(self) -> None:
        """Require bounded participation metadata without reopening a source."""
        operation = Budget()
        bound_text(self.identifier, operation)
        if not self.identifier or not isinstance(self.source, PreparedSource):
            raise InputError("Corpus documents need names and prepared sources")
        if self.graphs is not None:
            if type(self.graphs) is not tuple:
                raise InputError("Corpus graph participation requires a finite tuple")
            operation.consume("states", len(self.graphs))
            for graph in self.graphs:
                if graph is not None:
                    if not isinstance(graph, URIRef | BNode):
                        raise InputError(
                            "Corpus graph names require RDF resource terms"
                        )
                    bound_node(graph, operation)


@dataclass(frozen=True)
class CorpusOrigin:
    """Caller document association beside established logical/retrieval identities."""

    identifier: str
    document_iri: str | None
    effective_url: str | None


@dataclass(frozen=True)
class CorpusSnapshot:
    """Immutable exact authorized records, never caller graphs or literal slots."""

    origin: CorpusOrigin
    witness: SnapshotWitness
    literal_fidelity: str


@dataclass(frozen=True)
class CorpusContribution:
    """One original asserted quad and its independently supplied document origin."""

    origin: CorpusOrigin
    quad: PatchQuad


@dataclass(frozen=True)
class EntityDescription:
    """Same-IRI descriptions in one physical partition, or requested union scope.

    Blank references are existing portable document-local RDF tokens. Their
    spelling establishes no identity across independent documents.
    """

    subject: PatchResource
    graph: PatchResource | None
    contributions: tuple[CorpusContribution, ...]


@dataclass(frozen=True)
class CorpusIndex:
    """Owned exact snapshots and derived, graph-scoped description inventory."""

    snapshots: tuple[CorpusSnapshot, ...]
    entities: tuple[EntityDescription, ...]
    report: Report


@dataclass(frozen=True)
class PredicateAgreement:
    """Require single-valued agreement for a repeated named entity's predicate.

    Each describing document contributes at most one distinct selected value.
    required=True also forbids omission by any participating description. This
    application contract never asserts OWL functionality or inconsistency.
    """

    predicate: URIRef
    equality: Literal["rdf-term", "sparql-1.1", "owl2-datatype-map"] = "rdf-term"
    required: bool = True

    def __post_init__(self) -> None:
        """Require named predicate and explicit finite equality/omission policies."""
        operation = Budget()
        if not isinstance(self.predicate, URIRef):
            raise InputError("Agreement predicates require URIRef values")
        bound_node(self.predicate, operation)
        bound_text(self.equality, operation)
        if (
            self.equality not in ("rdf-term", "sparql-1.1", "owl2-datatype-map")
            or type(self.required) is not bool
        ):
            raise InputError("Unknown corpus predicate agreement policy")
        absolute_iri(self.predicate)


@dataclass(frozen=True)
class ConsolidationCheck:
    """A named required ordinary postcondition over a fresh candidate snapshot."""

    name: str
    check: Callable[[PreparedSource], Report]

    def __post_init__(self) -> None:
        """Keep callback execution explicit and require finite diagnostic metadata."""
        bound_text(self.name, Budget())
        if not self.name or not callable(self.check):
            raise InputError("Consolidation checks require names and callbacks")


@dataclass(frozen=True)
class Consolidation:
    """Only a fully qualified candidate is published; raw reports remain separate."""

    source: PreparedSource | None
    report: Report
    checks: tuple[Report, ...]
    index: CorpusIndex
    merge_report: Report | None = None
