"""Explicit source map keys, processed index modes and named-record bindings."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar, cast

from bijux_ontology._jsonpaths import SourcePath
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Report

T = TypeVar("T")


def _capture(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    """Own a bounded finite declaration inventory before consuming it."""
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), (str, bytes)
    ):
        raise InputError("Index contracts require finite typed sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        result.append(values[index])
    return tuple(result)


def _text(value: str, budget: Budget) -> None:
    """Bound scalar text before retained declaration or comparison work."""
    if not isinstance(value, str):
        raise InputError("Index contracts require text")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Index text exceeds max_bytes")
    budget.consume("work", len(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Index text requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Index text exceeds max_bytes")
    budget.consume("bytes", size)


@dataclass(frozen=True)
class IndexBinding:
    """Require one raw source key to identify a named record in an exact graph."""

    key: str
    identifier: str
    graph: str | None = None

    def __post_init__(self) -> None:
        operation = Budget()
        for value in (self.key, self.identifier, self.graph):
            if value is not None:
                _text(value, operation)
        absolute_iri(self.identifier)
        if self.graph is not None:
            absolute_iri(self.graph)


@dataclass(frozen=True)
class IndexMapExpectation:
    """Check one map's mapping, optional exact keys and processed index origin.

    index_property=None selects plain @index metadata. An explicit absolute IRI
    selects custom property indexing. Bindings concern source records; repeated
    RDF IDs do not establish distinguishable records or merge their source keys.
    """

    mapping: ContextExpectation
    keys: tuple[str, ...] | None = None
    index_property: str | None = None
    bindings: tuple[IndexBinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mapping, ContextExpectation):
            raise InputError("Index maps require an explicit ContextExpectation")
        operation = Budget()
        if self.keys is not None:
            keys = _capture(self.keys, operation)
            for key in keys:
                _text(key, operation)
            if len(set(keys)) != len(keys):
                raise InputError("Expected map keys must be distinct")
            object.__setattr__(self, "keys", keys)
        if self.index_property is not None:
            _text(self.index_property, operation)
            absolute_iri(self.index_property)
        bindings = _capture(self.bindings, operation)
        if any(not isinstance(binding, IndexBinding) for binding in bindings):
            raise InputError("Expected typed IndexBinding declarations")
        object.__setattr__(self, "bindings", bindings)


@dataclass(frozen=True)
class IndexMapObservation:
    """One raw key and its original graph/list-scoped processed occurrences."""

    source_key: str
    path: SourcePath
    usage: ObservedUsage
    qualified: bool


@dataclass(frozen=True)
class IndexMapCheck:
    """Keep completed key facts separate from required association qualification."""

    observations: tuple[IndexMapObservation, ...]
    report: Report
