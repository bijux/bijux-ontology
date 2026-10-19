"""Graph-qualified asserted entity facts within one owned expansion.

Anonymous tokens belong to this preparation only. This inventory joins RDF
identities; it performs no OWL equality, inequality or type inference.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.paths import SourcePath
from bijux_ontology.jsonld.observations import (
    ObservedOccurrence,
    ObservedUsage,
    occurrences,
)


@dataclass(frozen=True)
class EntityKey:
    """An effective subject and graph; absent IDs retain local anonymous tokens."""

    identifier: str | None
    anonymous: int | None = None
    graph: str | None = None
    anonymous_graph: bool = False
    graph_identity: int | None = None


@dataclass(frozen=True)
class EntityDescription:
    """Asserted descriptions joined only for exactly the same graph and subject."""

    key: EntityKey
    types: tuple[str, ...] = ()
    facts: tuple[ObservedOccurrence, ...] = ()

    @property
    def described(self) -> bool:
        """An identifier-only reference supplies no local description."""
        return bool(
            self.types or any(not fact.predicate.startswith("@") for fact in self.facts)
        )


def subject_key(item: ObservedOccurrence) -> EntityKey:
    """Identify an occurrence's owning subject in its original graph."""
    return EntityKey(
        item.subject,
        item.subject_identity,
        item.graph,
        item.anonymous_graph,
        item.graph_identity,
    )


def object_key(item: ObservedOccurrence) -> EntityKey | None:
    """Identify an RDF resource value, preserving anonymous object identity."""
    traits = item.traits
    if traits.kind not in {"node", "iri", "blank", "graph"}:
        return None
    return EntityKey(
        traits.identifier,
        item.identifier_identity,
        item.graph,
        item.anonymous_graph,
        item.graph_identity,
    )


class EntityInventory:
    """Read-only finite graph-scoped descriptions from the usage expansion."""

    def __init__(self, expanded: object, budget: Budget) -> None:
        types: dict[EntityKey, list[str]] = {}
        facts: dict[EntityKey, list[ObservedOccurrence]] = {}
        for item in occurrences(expanded):
            budget.consume("work")
            if item.predicate == "@id":
                key = EntityKey(
                    item.traits.identifier,
                    graph=item.graph,
                    anonymous_graph=item.anonymous_graph,
                    graph_identity=item.graph_identity,
                )
            else:
                key = subject_key(item)
            types.setdefault(key, [])
            facts.setdefault(key, [])
            if item.predicate in {"@id", "@type"}:
                facts[key].append(item)
            if item.predicate == "@type":
                value = item.traits.identifier
                if value is not None and value not in types[key]:
                    types[key].append(value)
            elif not item.predicate.startswith("@"):
                facts[key].append(item)
                target = object_key(item)
                if target is not None:
                    types.setdefault(target, [])
                    facts.setdefault(target, [])
        self._entities: Mapping[EntityKey, EntityDescription] = MappingProxyType(
            {
                key: EntityDescription(key, tuple(values), tuple(facts[key]))
                for key, values in types.items()
            }
        )

    @property
    def entities(self) -> tuple[EntityDescription, ...]:
        """Enumerate established entities without exposing mutable processor data."""
        return tuple(self._entities.values())

    def iter_entities(self, *, budget: Budget) -> Iterator[EntityDescription]:
        """Yield owned records under the caller's finite traversal allowance."""
        budget.reserve_storage(128)
        for entity in self._entities.values():
            budget.consume("work")
            budget.consume("states")
            budget.reserve_storage(64)
            yield entity

    def get(self, key: EntityKey) -> EntityDescription | None:
        """Look up exact RDF identity without graph union or OWL inference."""
        return self._entities.get(key)


@dataclass(frozen=True)
class SourceEntity:
    """One established source object, distinct from its joined RDF entity."""

    path: SourcePath
    key: EntityKey
    described: bool


def source_entity(
    node: dict[str, object],
    path: SourcePath,
    observe: Callable[[SourcePath], ObservedUsage],
    budget: Budget,
) -> SourceEntity | None:
    """Establish the object's own subject without treating wrappers as children."""
    own: set[EntityKey] = set()
    outgoing: list[ObservedOccurrence] = []
    incoming = observe(path) if path else None
    for key, raw in node.items():
        budget.consume("work")
        if key == "@context":
            continue
        try:
            usage = observe((*path, key))
        except LimitExceeded:
            raise
        except InputError:
            continue
        if usage.limitation or not usage.occurrences:
            continue
        depth = min(item.depth for item in usage.occurrences)
        primitive = (
            isinstance(raw, str)
            or isinstance(raw, list)
            and all(isinstance(value, str) for value in raw)
        )
        for item in usage.occurrences:
            budget.consume("work")
            if item.depth != depth:
                continue
            if item.predicate == "@id" and primitive:
                own.add(
                    EntityKey(
                        item.traits.identifier,
                        graph=item.graph,
                        anonymous_graph=item.anonymous_graph,
                        graph_identity=item.graph_identity,
                    )
                )
            elif item.predicate == "@type" and primitive:
                own.add(subject_key(item))
                outgoing.append(item)
            elif not item.predicate.startswith("@"):
                outgoing.append(item)
    if not own and incoming is not None and not incoming.limitation:
        for item in outgoing:
            for edge in incoming.occurrences:
                budget.consume("work")
                if item.depth > edge.depth and subject_key(item) == object_key(edge):
                    own.add(subject_key(item))
    if not own and ("@type" in node or "@id" in node):
        # Canonical node keywords exclude list/set wrappers even when removing
        # the whole anonymous node is ambiguous. An independently associated
        # own field still establishes its original subject token.
        own.update(
            subject_key(item) for item in outgoing if not item.predicate.startswith("@")
        )
    if len(own) != 1:
        return None
    selected_key = next(iter(own))
    return SourceEntity(
        path, selected_key, any(subject_key(item) == selected_key for item in outgoing)
    )
