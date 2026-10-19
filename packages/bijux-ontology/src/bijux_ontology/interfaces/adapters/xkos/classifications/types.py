"""Finite asserted classification records and explicit application contracts."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import BNode, Literal, Namespace, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report

XKOS = Namespace("http://rdf-vocabulary.ddialliance.org/xkos#")
XKOS_VERSION = "xkos-1.2"
Resource = URIRef | BNode
RelationKind = Choice["generic", "partitive", "skos"]
Statement = tuple[Node, Node, Node]


@dataclass(frozen=True)
class ClassificationLevel:
    """One authored ordered occurrence, preserving ambiguous raw declarations."""

    identifier: Resource
    position: int
    depths: tuple[Node, ...]
    members: tuple[Node, ...]
    patterns: tuple[Node, ...]
    organized_by: tuple[Node, ...]


@dataclass(frozen=True)
class CategoryRow:
    """A selected table/code-list row with original RDF code and label terms."""

    category: Resource
    level: Resource
    position: int
    codes: tuple[Literal, ...]
    labels: tuple[Literal, ...]


@dataclass(frozen=True)
class HierarchyLink:
    """Normalized direction retains original assertion and relationship meaning."""

    child: Node
    parent: Node
    kind: RelationKind
    statement: Statement


@dataclass(frozen=True, init=False)
class ClassificationView:
    """Owned one-graph inventory, not a global classification validity claim."""

    source: PreparedSource | None
    graph: Resource | None
    scheme: Resource
    metadata: SnapshotMetadata | None
    ordered_levels: tuple[Resource, ...]
    levels: tuple[ClassificationLevel, ...]
    categories: tuple[Resource, ...]
    categories_explicit: bool
    rows: tuple[CategoryRow, ...]
    hierarchy: tuple[HierarchyLink, ...]
    coverage: tuple[Statement, ...]
    number_of_levels: tuple[Node, ...]
    report: Report

    def __init__(self) -> None:
        """Require bounded source inspection before publishing an owned view."""
        raise InputError("Use read_classification for an owned classification view")

    @classmethod
    def _owned(
        cls,
        source: PreparedSource | None,
        graph: Resource | None,
        scheme: Resource,
        metadata: SnapshotMetadata | None,
        ordered_levels: tuple[Resource, ...],
        levels: tuple[ClassificationLevel, ...],
        categories: tuple[Resource, ...],
        categories_explicit: bool,
        rows: tuple[CategoryRow, ...],
        hierarchy: tuple[HierarchyLink, ...],
        coverage: tuple[Statement, ...],
        number_of_levels: tuple[Node, ...],
        report: Report,
    ) -> "ClassificationView":
        """Adopt qualified source records without recopying their RDF identities."""
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("graph", graph),
            ("scheme", scheme),
            ("metadata", metadata),
            ("ordered_levels", ordered_levels),
            ("levels", levels),
            ("categories", categories),
            ("categories_explicit", categories_explicit),
            ("rows", rows),
            ("hierarchy", hierarchy),
            ("coverage", coverage),
            ("number_of_levels", number_of_levels),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class LevelCodePolicy:
    """Select exact allowed code evidence or a caller-chosen declared dialect.

    XKOS does not specify a regex dialect. A declared notationPattern is never
    interpreted unless re2-fullmatch is explicitly selected by the caller.
    """

    level: Resource
    required: bool = True
    unique: bool = True
    allowed: ValueEnumeration | None = None
    declared_pattern_dialect: Choice["re2-fullmatch"] | None = None

    def __post_init__(self) -> None:
        """Require typed finite policies rather than arbitrary predicates."""
        if (
            not isinstance(self.level, URIRef | BNode)
            or type(self.required) is not bool
            or type(self.unique) is not bool
        ):
            raise InputError("Code policies require a resource and boolean rules")
        if self.allowed is not None and not isinstance(self.allowed, ValueEnumeration):
            raise InputError("Allowed codes require finite ValueEnumeration evidence")
        if self.declared_pattern_dialect not in (None, "re2-fullmatch"):
            raise InputError("Select the explicit re2-fullmatch application dialect")


@dataclass(frozen=True)
class PartitionContract:
    """Finite supplied units and assignments, never an unspecified world domain."""

    levels: tuple[Resource, ...]
    population: tuple[Resource, ...] | None = None
    assignments_complete: bool = False
    exhaustive: bool = True
    mutually_exclusive: bool = True
    assignment_predicates: tuple[URIRef, ...] = (XKOS.classifiedUnder,)
    propagation: tuple[RelationKind, ...] = ()

    def __post_init__(self) -> None:
        """Require explicit immutable selection and at least one finite obligation."""
        if type(self.levels) is not tuple or any(
            not isinstance(x, URIRef | BNode) for x in self.levels
        ):
            raise InputError("Partition levels require a finite resource tuple")
        if self.population is not None and (
            type(self.population) is not tuple
            or any(not isinstance(x, URIRef | BNode) for x in self.population)
        ):
            raise InputError("Partition population requires a finite resource tuple")
        if any(
            type(x) is not bool
            for x in (
                self.assignments_complete,
                self.exhaustive,
                self.mutually_exclusive,
            )
        ) or not (self.exhaustive or self.mutually_exclusive):
            raise InputError(
                "Select boolean finite coverage or exclusivity obligations"
            )
        if (
            type(self.assignment_predicates) is not tuple
            or not self.assignment_predicates
            or any(type(x) is not URIRef for x in self.assignment_predicates)
        ):
            raise InputError("Select finite exact assignment predicate IRIs")
        if type(self.propagation) is not tuple or any(
            x not in ("generic", "partitive", "skos") for x in self.propagation
        ):
            raise InputError(
                "Select explicit hierarchy kinds for assignment propagation"
            )


@dataclass(frozen=True)
class ClassificationPolicy:
    """Application rules are opt-in and cannot invalidate ordinary SKOS globally."""

    required_levels: tuple[Resource, ...] = ()
    require_depth: bool = True
    unique_level: bool = True
    tree_relations: tuple[RelationKind, ...] = ()
    adjacent_levels: bool = True
    codes: tuple[LevelCodePolicy, ...] = ()
    partition: PartitionContract | None = None

    def __post_init__(self) -> None:
        """Reject mutable or ambiguous policy shapes before inspecting RDF."""
        if type(self.required_levels) is not tuple or any(
            not isinstance(x, URIRef | BNode) for x in self.required_levels
        ):
            raise InputError("Required classification levels need a resource tuple")
        if any(
            type(x) is not bool
            for x in (self.require_depth, self.unique_level, self.adjacent_levels)
        ):
            raise InputError("Classification checks require explicit booleans")
        if type(self.tree_relations) is not tuple or any(
            x not in ("generic", "partitive", "skos") for x in self.tree_relations
        ):
            raise InputError("Select explicit relation kinds for the tree policy")
        if type(self.codes) is not tuple or any(
            type(x) is not LevelCodePolicy for x in self.codes
        ):
            raise InputError("Classification codes require finite typed policies")
        if self.partition is not None and type(self.partition) is not PartitionContract:
            raise InputError("Partition checking requires an explicit finite contract")


@dataclass(frozen=True)
class ClassificationCheck:
    """Selected table rows and separate finite assignment proof, when requested."""

    view: ClassificationView
    rows: tuple[CategoryRow, ...]
    partition: Report | None
    report: Report
