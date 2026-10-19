"""Checked statement-annotation mappings with exact original dataset ownership."""

from dataclasses import dataclass

import pyoxigraph as ox
from rdflib import BNode, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.patches.types import PatchBlank, SnapshotWitness
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report

REIFIER_CONTRACT = "rdf12-cr-20260407-reifiers-pyoxigraph-0.5.12"
REIFIER_CONTRACTS = (
    "rdf12-cr-20260407-reifiers-pyoxigraph-0.5.11",
    REIFIER_CONTRACT,
)


@dataclass(frozen=True)
class ReifierSelection:
    """One original resource occurrence in an explicit physical graph.

    None denotes the default graph, including an ordinary standalone graph.
    Blank resources and graph names require actual objects owned by the source.
    """

    node: URIRef | BNode
    graph: URIRef | BNode | None = None

    def __post_init__(self) -> None:
        """Exclude literal selectors and implicit graph unions."""
        if not isinstance(self.node, URIRef | BNode) or (
            self.graph is not None and not isinstance(self.graph, URIRef | BNode)
        ):
            raise InputError("Reifier selections require resource and graph terms")


@dataclass(frozen=True)
class ReifierBlank:
    """An established original blank reference and its converted native identity."""

    original: BNode
    reference: PatchBlank
    converted: ox.BlankNode


@dataclass(frozen=True, init=False)
class ReifierDataset:
    """Factory-owned finite conversion candidate, not a general RDF 1.2 loader.

    Quads retain actual maintained native terms. Named-graph inventory separately
    preserves empty graphs which N-Quads cannot express. Correspondence belongs
    to this conversion only; labels in another parser capture prove no identity.
    The original source is metadata provenance, never inverse reconstruction data.
    """

    quads: tuple[ox.Quad, ...]
    graphs: tuple[ox.NamedNode | ox.BlankNode, ...]
    selections: tuple[
        tuple[ox.NamedNode | ox.BlankNode, ox.NamedNode | ox.BlankNode | None], ...
    ]
    blanks: tuple[ReifierBlank, ...]
    base: SnapshotWitness
    origin: PreparedSource
    metadata: SnapshotMetadata
    content: bytes
    contract: str

    def __init__(self) -> None:
        """Require an actual checked conversion rather than declarative records."""
        raise InputError("Reifier datasets require convert_reifications")

    @classmethod
    def _from_owned(
        cls,
        quads: tuple[ox.Quad, ...],
        graphs: tuple[ox.NamedNode | ox.BlankNode, ...],
        selections: tuple[
            tuple[ox.NamedNode | ox.BlankNode, ox.NamedNode | ox.BlankNode | None], ...
        ],
        blanks: tuple[ReifierBlank, ...],
        base: SnapshotWitness,
        origin: PreparedSource,
        metadata: SnapshotMetadata,
        content: bytes,
        contract: str,
    ) -> "ReifierDataset":
        """Publish only prequalified immutable terms and native round-trip output."""
        result = object.__new__(cls)
        for name, value in (
            ("quads", quads),
            ("graphs", graphs),
            ("selections", selections),
            ("blanks", blanks),
            ("base", base),
            ("origin", origin),
            ("metadata", metadata),
            ("content", content),
            ("contract", contract),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class ReificationConversion:
    """A checked mapping candidate or explicit incomplete/failed acceptance."""

    snapshot: ReifierDataset | None
    report: Report
