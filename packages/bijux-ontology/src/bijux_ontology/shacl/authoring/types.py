"""Finite caller-authored SHACL Core constraints and explicit shape references."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from rdflib import BNode, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.paths import Path

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.paths import RdfPath, capture_path

ShapeIdentifier: TypeAlias = URIRef | BNode


def _inventories(*values: object) -> None:
    """Require finite immutable record containers before later compilation."""
    for value in values:
        if type(value) is not tuple or len(value) > Limits().max_states:
            raise InputError("SHACL inventories require bounded immutable tuples")


@dataclass(frozen=True)
class ShaclShapeRef:
    """Refer to a supplied definition without recursively expanding Python objects."""

    identifier: ShapeIdentifier


@dataclass(frozen=True)
class ShaclValue:
    """Conjoin explicit Core value rules; no ontology constraint is inferred."""

    datatype: str | None = None
    class_iri: str | None = None
    node_kind: (
        Literal[
            "iri",
            "blank",
            "literal",
            "blank_or_iri",
            "blank_or_literal",
            "iri_or_literal",
        ]
        | None
    ) = None
    has_value: URIRef | RDFLiteral | Absent = ABSENT
    in_values: tuple[URIRef | RDFLiteral, ...] | None = None

    def __post_init__(self) -> None:
        """Retain a bounded immutable enumeration when explicitly supplied."""
        if self.in_values is not None:
            _inventories(self.in_values)


@dataclass(frozen=True)
class ShaclCount:
    """Bound the actual value count of a property shape."""

    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ShaclComparison:
    """Compare property-shape values with one explicit counterpart predicate."""

    operator: Literal["equals", "disjoint", "less_than", "less_than_or_equals"]
    other: str


@dataclass(frozen=True)
class ShaclQualifiedCount:
    """Count values conforming to a referenced shape, with explicit sibling policy."""

    shape: ShaclShapeRef
    minimum: int | None = None
    maximum: int | None = None
    disjoint: bool = False


@dataclass(frozen=True)
class ShaclLogical:
    """Combine referenced shapes using a finite Core logical operator."""

    operator: Literal["and", "or", "xone", "not"]
    shapes: tuple[ShaclShapeRef, ...]

    def __post_init__(self) -> None:
        """Require an immutable finite reference inventory."""
        _inventories(self.shapes)


ShaclConstraint: TypeAlias = (
    ShaclValue
    | ShaclCount
    | ShaclComparison
    | ShaclQualifiedCount
    | ShaclLogical
    | ShaclShapeRef
)


@dataclass(frozen=True)
class ShaclNodeShape:
    """Define a node shape with explicit targets and referenced property shapes."""

    identifier: ShapeIdentifier
    properties: tuple[ShaclShapeRef, ...] = ()
    constraints: tuple[ShaclConstraint, ...] = ()
    target_classes: tuple[str, ...] = ()
    target_nodes: tuple[URIRef | RDFLiteral, ...] = ()

    def __post_init__(self) -> None:
        """Require bounded explicit targets, properties and constraint inventories."""
        _inventories(
            self.properties, self.constraints, self.target_classes, self.target_nodes
        )


@dataclass(frozen=True)
class ShaclPropertyShape:
    """Define a reusable property shape over one maintained or captured RDF path."""

    identifier: ShapeIdentifier
    path: URIRef | Path | RdfPath
    constraints: tuple[ShaclConstraint, ...] = ()

    def __post_init__(self) -> None:
        """Own path intent and require a bounded immutable constraint inventory."""
        _inventories(self.constraints)
        object.__setattr__(self, "path", capture_path(self.path))
