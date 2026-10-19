"""Grouped XKOS associations and explicit particular-record conversion choices."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, Namespace, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.migrations.values.types import ValueMap
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.inspection import VocabularyView

XKOS = Namespace("http://rdf-vocabulary.ddialliance.org/xkos#")
Resource = URIRef | BNode
Statement = tuple[Node, Node, Node]
AssociationKind = Literal[
    "one-to-one", "split", "merge", "many-to-many", "creation", "deletion", "empty"
]


@dataclass(frozen=True)
class ClassificationRelease:
    """An exact version-scheme IRI and one supplied physical vocabulary scope.

    Completeness qualifies absence only within this supplied member inventory.
    A name, notation, prefix or equal category IRI in a different release does
    not establish membership in this scheme. No temporal version is inferred.
    """

    scheme: URIRef
    vocabulary: VocabularyView
    complete: bool


@dataclass(frozen=True)
class AssociationRequirement:
    """An optional exact required group inventory, including every side member."""

    association: Resource
    sources: tuple[URIRef, ...]
    targets: tuple[URIRef, ...]


@dataclass(frozen=True)
class ConceptAssociation:
    """One original group; the side sets are never expanded into pairwise mappings."""

    identifier: Resource
    types: tuple[Node, ...]
    sources: tuple[Node, ...]
    targets: tuple[Node, ...]
    kind: AssociationKind


@dataclass(frozen=True)
class _ReleaseEvidence:
    """Existing vocabulary membership and immutable scope receipt, not a new index."""

    release: ClassificationRelease
    source: PreparedSource
    metadata: SnapshotMetadata
    members: frozenset[URIRef]


@dataclass(frozen=True, init=False, eq=False)
class Correspondence:
    """One typed table plus its complete original backing source and exact pin."""

    identifier: Resource
    types: tuple[Node, ...]
    compares: tuple[Node, ...]
    links: tuple[Node, ...]
    associations: tuple[ConceptAssociation, ...]
    source_release: ClassificationRelease
    target_release: ClassificationRelease
    source: PreparedSource | None
    graph: Resource | None
    report: Report
    _metadata: SnapshotMetadata | None
    _releases: tuple[_ReleaseEvidence, ...]

    def __init__(self) -> None:
        """Require qualified reading to associate source ownership with records."""
        raise InputError("Use read_correspondence")

    @classmethod
    def _owned(
        cls,
        identifier: Resource,
        types: tuple[Node, ...],
        compares: tuple[Node, ...],
        links: tuple[Node, ...],
        associations: tuple[ConceptAssociation, ...],
        source_release: ClassificationRelease,
        target_release: ClassificationRelease,
        source: PreparedSource | None,
        graph: Resource | None,
        report: Report,
        metadata: SnapshotMetadata | None,
        releases: tuple[_ReleaseEvidence, ...],
    ) -> "Correspondence":
        """Adopt only the already bounded original records and scope receipts."""
        result = object.__new__(cls)
        for name, value in (
            ("identifier", identifier),
            ("types", types),
            ("compares", compares),
            ("links", links),
            ("associations", associations),
            ("source_release", source_release),
            ("target_release", target_release),
            ("source", source),
            ("graph", graph),
            ("report", report),
            ("_metadata", metadata),
            ("_releases", releases),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class CorrespondenceExport:
    """Fresh reconstructed grouped RDF, without an invented equivalence projection."""

    source: PreparedSource | None
    report: Report


@dataclass(frozen=True)
class RecordConversion:
    """A caller-authored exact value map for one record and one actual group.

    No target, count distribution or weight is inferred. The existing ValueMap
    owns exact term conversion intent; targets must be named destination members.
    """

    record: Resource
    association: Resource
    values: ValueMap


@dataclass(frozen=True)
class AssignmentResolution:
    """An actual source assignment and its explicit checked destination choice."""

    record: Resource
    category: Node
    associations: tuple[ConceptAssociation, ...]
    target: URIRef | None
    status: Literal["resolved", "requires-policy", "rejected"]


@dataclass(frozen=True)
class AssignmentAssessment:
    """Finite observed assignments; unresolved choices never become migrated data."""

    source: PreparedSource | None
    graph: Resource | None
    predicate: URIRef
    resolutions: tuple[AssignmentResolution, ...]
    report: Report
