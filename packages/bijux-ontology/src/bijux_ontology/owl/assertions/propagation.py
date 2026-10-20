"""Finite identity consequences scheduled by actual changed endpoint classes."""

from collections import deque
from itertools import chain, combinations, product

from rdflib import BNode, URIRef

from .consequences import functional_pair, key_identities
from .context import AssertionContext, _Row
from .patterns import KeyPattern
from .types import TermKey


class FunctionalBuckets:
    """Join newly coincident buckets without repeating completed property pairs."""

    def __init__(
        self,
        state: AssertionContext,
        functional: tuple[URIRef, ...],
        inverse: tuple[URIRef, ...],
    ) -> None:
        self.state = state
        self.rules: list[tuple[URIRef, bool, bool]] = []
        self.buckets: list[dict[TermKey, list[_Row]]] = []
        self.owners: dict[TermKey, set[int]] = {}
        self.pending: deque[tuple[int, _Row, _Row]] = deque()
        for predicate, inverse_ in (
            *((p, False) for p in functional),
            *((p, True) for p in inverse),
        ):
            kind, _ = state.kind(predicate)
            if kind is None or inverse_ and kind != "object":
                state.unknown(
                    "Property functionality lacks a supported logical category"
                )
                continue
            state.operation.consume("states", 2)
            state.operation.reserve_storage(256)
            rule = len(self.rules)
            self.rules.append((predicate, inverse_, kind == "data"))
            groups: dict[TermKey, list[_Row]] = {}
            self.buckets.append(groups)
            for row in state.rows(predicate):
                endpoint = row.value if inverse_ else row.subject
                assert isinstance(endpoint, URIRef | BNode)
                representative = state.identity.find(state.key(endpoint))
                state.operation.consume("states", 2)
                state.operation.reserve_storage(192)
                groups.setdefault(representative, []).append(row)
                self.owners.setdefault(representative, set()).add(rule)
            for rows in groups.values():
                pairs = (
                    combinations(rows, 2)
                    if kind == "data"
                    else ((rows[0], right) for right in rows[1:])
                )
                for left, right in pairs:
                    self.schedule(rule, left, right)

    def schedule(self, rule: int, left: _Row, right: _Row) -> None:
        """Reserve each finite pending inference before retaining its original rows."""
        self.state.operation.consume("work")
        self.state.operation.consume("states")
        self.state.operation.reserve_storage(96)
        self.pending.append((rule, left, right))

    def merged(self, surviving: TermKey, removed: TermKey) -> None:
        """Only the properties using a merged class receive new obligations."""
        for rule in self.owners.pop(removed, ()):
            self.state.operation.consume("work")
            groups = self.buckets[rule]
            incoming = groups.pop(removed)
            current = groups.get(surviving)
            if current is None:
                groups[surviving] = incoming
            else:
                if self.rules[rule][2]:
                    for left, right in product(current, incoming):
                        self.schedule(rule, left, right)
                else:
                    self.schedule(rule, current[0], incoming[0])
                self.state.operation.consume("states", len(incoming))
                self.state.operation.reserve_storage(16 * len(incoming))
                current.extend(incoming)
            self.state.operation.consume("states")
            self.state.operation.reserve_storage(64)
            self.owners.setdefault(surviving, set()).add(rule)


def close_identities(
    state: AssertionContext,
    functional: tuple[URIRef, ...],
    inverse: tuple[URIRef, ...],
    keys: tuple[KeyPattern, ...],
) -> None:
    """Run fixed finite rules until actual identity merges enable no more work."""
    properties = FunctionalBuckets(state, functional, inverse)
    owners: dict[TermKey, set[int]] = {}
    for index, key in enumerate(keys):
        resources = chain(
            (subject for subject, _, _ in state.types),
            (
                resource
                for predicate in key.properties
                for row in state.rows(predicate)
                for resource in (row.subject, row.value)
                if isinstance(resource, URIRef | BNode)
            ),
        )
        for resource in resources:
            state.operation.consume("work")
            representative = state.identity.find(state.key(resource))
            state.operation.consume("states")
            state.operation.reserve_storage(64)
            owners.setdefault(representative, set()).add(index)
    pending_keys = deque(range(len(keys)))
    queued = set(pending_keys)
    state.operation.consume("states", 2 * len(keys))
    state.operation.reserve_storage(128 + 64 * len(keys))
    cursor = len(state.identity.merges)
    while properties.pending or pending_keys:
        state.operation.consume("work")
        if properties.pending:
            rule, left, right = properties.pending.popleft()
            predicate, inverse_, _ = properties.rules[rule]
            functional_pair(state, predicate, inverse_, left, right)
        else:
            index = pending_keys.popleft()
            queued.remove(index)
            key_identities(state, (keys[index],))
        while cursor < len(state.identity.merges):
            state.operation.consume("work")
            surviving, removed = state.identity.merges[cursor]
            cursor += 1
            properties.merged(surviving, removed)
            affected = owners.pop(removed, set())
            affected.update(owners.get(surviving, ()))
            if affected:
                state.operation.consume("states", len(affected))
                state.operation.reserve_storage(64 * len(affected))
                owners[surviving] = affected
                state.operation.consume(
                    "work", len(affected) * max(1, len(affected).bit_length())
                )
                state.operation.reserve_storage(16 * len(affected))
                for index in sorted(affected):
                    state.operation.consume("work")
                    if index not in queued:
                        state.operation.consume("states", 2)
                        state.operation.reserve_storage(64)
                        pending_keys.append(index)
                        queued.add(index)
