"""Graph-qualified asserted entity facts within one owned expansion.

Anonymous tokens belong to this preparation only. This inventory joins RDF
identities; it performs no OWL equality, inequality or type inference.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bijux_ontology.limits import Budget

from .observations import ObservedOccurrence, occurrences


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

    def get(self, key: EntityKey) -> EntityDescription | None:
        """Look up exact RDF identity without graph union or OWL inference."""
        return self._entities.get(key)
