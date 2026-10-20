"""Explicit instance identities, release policies and exact comparison evidence."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.paths import Path

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchQuad,
    PatchResource,
    PatchValue,
)
from bijux_ontology.rdf.paths.types import RdfPath
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.value_comparison.types import LiteralValueComparison


@dataclass(frozen=True)
class InstanceEntity:
    """One actual subject in one physical graph, never a union-derived record."""

    subject: URIRef | BNode
    graph: GraphSelection | None = None


@dataclass(frozen=True)
class InstanceRelease:
    """An owned RDF corpus and the explicitly selected entity inventory."""

    source: PreparedSource
    entities: tuple[InstanceEntity, ...]


@dataclass(frozen=True)
class InstanceEntityPair:
    """Caller-established record identity; an IRI rename is otherwise add/remove."""

    before: InstanceEntity
    after: InstanceEntity


@dataclass(frozen=True)
class InstanceFieldPolicy:
    """Finite path policy, with ordered-list interpretation explicitly selected.

    Immutable and append-only apply to existing records; new records may introduce
    values. Value equality qualifies acceptance without rewriting the exact delta.
    """

    name: str
    path: URIRef | Path | RdfPath
    change: Literal["immutable", "append-only", "allow"] = "immutable"
    equality: Literal["rdf-term", "sparql-1.1", "owl2-datatype-map"] = "rdf-term"
    collection: Literal["set", "list"] = "set"


@dataclass(frozen=True)
class InstanceFieldChange:
    """Original entity/path evidence, exact values and qualified policy decision."""

    before: InstanceEntity | None
    after: InstanceEntity | None
    policy: InstanceFieldPolicy
    path: RdfPath
    before_values: tuple[PatchValue, ...]
    after_values: tuple[PatchValue, ...]
    before_support: tuple[PatchQuad, ...]
    after_support: tuple[PatchQuad, ...]
    exact_equal: bool | None
    accepted: bool | None
    comparisons: tuple[LiteralValueComparison, ...] = ()


@dataclass(frozen=True)
class InstanceSharedNodeChange:
    """Changed exact references or description of a multiply referenced resource."""

    node: PatchResource
    before_incoming: tuple[PatchQuad, ...]
    after_incoming: tuple[PatchQuad, ...]
    before_outgoing: tuple[PatchQuad, ...]
    after_outgoing: tuple[PatchQuad, ...]


@dataclass(frozen=True)
class InstanceReleaseComparison:
    """A reusable exact dataset delta beside independently evaluated change rules."""

    before: InstanceRelease
    after: InstanceRelease
    patch: DatasetPatch | None
    added_entities: tuple[InstanceEntity, ...]
    removed_entities: tuple[InstanceEntity, ...]
    fields: tuple[InstanceFieldChange, ...]
    shared_nodes: tuple[InstanceSharedNodeChange, ...]
    report: Report
