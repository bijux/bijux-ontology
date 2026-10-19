"""Finite traversal declarations and graph-qualified selection records."""

from dataclasses import KW_ONLY, dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.json.paths import SourcePath, validate_path
from bijux_ontology.jsonld.entities import EntityKey
from bijux_ontology.rdf.identifiers import absolute_iri

CoverageUnit = Literal["source_occurrence", "rdf_entity"]


def _bounds(minimum: int, maximum: int | None) -> None:
    if (
        type(minimum) is not int
        or minimum < 0
        or maximum is not None
        and (type(maximum) is not int or maximum < minimum)
    ):
        raise InputError("Selector bounds require 0 <= minimum <= maximum")


@dataclass(frozen=True)
class Property:
    """Select a concrete property, bounding matches separately for each parent."""

    key: str
    minimum: int = 0
    maximum: int | None = 1

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise InputError("A property selector requires a string key")
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class ArrayItems:
    """Select each actual array member, without treating objects as arrays."""

    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class KeyedItems:
    """Select members of an explicitly declared keyed source container.

    None selects all supplied keys. A finite tuple selects only those exact keys;
    these keys are source syntax, not inferred language/index/identity semantics.
    """

    keys: tuple[str, ...] | None = None
    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        if self.keys is not None and (
            not isinstance(self.keys, tuple)
            or any(not isinstance(key, str) for key in self.keys)
        ):
            raise InputError("Keyed containers require a tuple of string keys")
        _bounds(self.minimum, self.maximum)


Step = Property | ArrayItems | KeyedItems


def _steps(steps: tuple[Step, ...]) -> None:
    if not isinstance(steps, tuple) or any(
        not isinstance(step, (Property, ArrayItems, KeyedItems)) for step in steps
    ):
        raise InputError("Expected typed finite selector steps")


@dataclass(frozen=True)
class SourceSelector:
    """Traverse source occurrences with aggregate and per-parent bounds.

    Each step's bounds apply universally to its selected parents. Selector bounds
    apply to the final aggregate. minimum=0 explicitly permits an empty selection.
    """

    steps: tuple[Step, ...]
    start: SourcePath = ()
    _: KW_ONLY
    minimum: int = 1
    maximum: int | None = None

    def __post_init__(self) -> None:
        _steps(self.steps)
        validate_path(self.start)
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class EntitySelector:
    """Select finite expanded identities/asserted types in exactly one graph.

    Types match asserted membership, never entailed classification. exact_types
    additionally rejects other asserted types. Source coverage selects each actual
    describing object separately before following steps; pure references count
    only when include_references=True. Entity coverage joins all its descriptions
    and returns EntityKeys, with no fabricated source pointer.
    """

    identifiers: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    _: KW_ONLY
    graph: str | None
    coverage_unit: CoverageUnit = "rdf_entity"
    exact_types: bool = False
    include_references: bool = False
    steps: tuple[Step, ...] = ()
    minimum: int = 1
    maximum: int | None = None

    def __post_init__(self) -> None:
        for values in (self.identifiers, self.types):
            if not isinstance(values, tuple):
                raise InputError("Identity and asserted-type filters require tuples")
            for value in values:
                absolute_iri(value)
        if self.graph is not None:
            absolute_iri(self.graph)
        if self.coverage_unit not in ("source_occurrence", "rdf_entity"):
            raise InputError("Unknown selector coverage unit")
        if (
            type(self.exact_types) is not bool
            or type(self.include_references) is not bool
        ):
            raise InputError("Selector policies must be booleans")
        _steps(self.steps)
        if self.coverage_unit == "rdf_entity" and self.steps:
            raise InputError(
                "Source traversal steps require source occurrence coverage"
            )
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class Selection:
    """Concrete matches and truthful coverage, separate from semantic checking."""

    coverage_unit: CoverageUnit
    paths: tuple[SourcePath, ...]
    entities: tuple[EntityKey, ...]
    report: Report

    @property
    def count(self) -> int:
        """Count the explicitly chosen coverage unit, not RDF/source interchangeably."""
        return (
            len(self.paths)
            if self.coverage_unit == "source_occurrence"
            else len(self.entities)
        )
