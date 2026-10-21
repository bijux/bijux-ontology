"""Qualified topology record admission and exact corpus completeness."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal as Choice
from typing import cast

from rdflib import URIRef

from bijux_ontology.data.records.reading import RecordPresence, RecordReader
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.paths import bound_index, bounded_location
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import TopologyRecord


@dataclass(frozen=True)
class _Focus:
    """One qualified record binding and bounded caller diagnostic anchor."""

    record: TopologyRecord
    where: str


def capture_inventory(
    values: object, expected: type[object], budget: Budget, *, count: int | None = None
) -> tuple[object, ...]:
    """Own a bounded finite input inventory before semantic work begins."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Topology inventories require finite typed sequences")
    count = len(values) if count is None else count
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError("A topology inventory changed during capture") from error
        if not isinstance(value, expected):
            raise InputError("A topology inventory contains an unsupported item")
        if expected is str:
            bound_text(cast(str, value), budget)
            value = str(value)
        result.append(value)
    return tuple(result)


def _place(record: TopologyRecord, budget: Budget) -> str:
    """Bound known output metadata without inventing a source occurrence."""
    bound_text(record.identifier, budget)
    bound_node(record.subject, budget)
    location = record.location
    if location is None:
        return record.identifier
    for value in (location.source, location.term, location.graph, location.model):
        if value is not None:
            bound_text(value, budget)
    if location.branch is not None:
        bound_index(location.branch, budget)
    pointer = bounded_location(location.path, budget)
    size = len(location.source) + len(pointer)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Topology output location exceeds max_bytes")
    budget.consume("work", size)
    budget.consume("bytes", 4 * size)
    if (
        len(location.source.encode("utf-8")) + len(pointer.encode("utf-8"))
        > budget.limits.max_bytes
    ):
        raise LimitExceeded("Topology output location exceeds max_bytes")
    return (
        location.source + pointer
        or location.model
        or location.term
        or location.graph
        or record.identifier
    )


@dataclass
class RecordCorpus:
    """Original admitted records beside declared and observed inventory coverage.

    Admission retains its progress independently of path discovery, so a later
    stop cannot erase already qualified missing or foreign-binding outcomes.
    """

    reader: RecordReader
    roots: tuple[str, ...]
    shared_targets: tuple[str, ...]
    complete: bool
    closure: Choice["open", "closed"]
    focuses: dict[str, _Focus] = field(default_factory=dict)
    nodes: dict[tuple[str | None, ...], _Focus] = field(default_factory=dict)
    bindings: set[object] = field(default_factory=set)
    root_set: set[str] = field(init=False)
    shared_set: set[str] = field(init=False)
    unknown_inventory: bool = False
    checked: int = 0

    @property
    def closed(self) -> bool:
        """Whether this supplied inventory supports closed-boundary conclusions."""
        return self.complete and self.closure == "closed" and not self.unknown_inventory

    def admit(
        self, records: tuple[TopologyRecord, ...], operation: Budget
    ) -> Iterator[tuple[_Focus, RecordPresence]]:
        """Qualify exact record ownership without reading relation paths."""
        for record in records:
            where = _place(record, operation)
            if record.identifier in self.focuses:
                raise InputError("Topology record identifiers must be unique")
            operation.consume("states")
            focus = _Focus(record, where)
            self.focuses[record.identifier] = focus
            binding = (
                ("iri", str(record.subject))
                if isinstance(record.subject, URIRef)
                else ("blank", id(record.subject))
            )
            if binding in self.bindings:
                raise InputError("Topology record subjects must be unique")
            operation.consume("states")
            self.bindings.add(binding)
            presence = self.reader.presence(record.subject, operation)
            present, owned = presence.present, presence.owned
            if not owned:
                self.unknown_inventory = True
                self.checked += 1
                yield focus, presence
                continue
            if not present:
                unknown = not (self.complete and self.closure == "closed")
                self.unknown_inventory = self.unknown_inventory or unknown
                self.checked += 1
                yield focus, presence
                continue
            key = term_key(record.subject)
            if key in self.nodes:
                raise InputError("Topology record subjects must be unique")
            operation.consume("states")
            self.nodes[key] = focus
        if (
            set(self.roots) - self.focuses.keys()
            or set(self.shared_targets) - self.focuses.keys()
        ):
            raise InputError("Roots and shared targets must identify supplied records")
        self.root_set, self.shared_set = set(self.roots), set(self.shared_targets)
