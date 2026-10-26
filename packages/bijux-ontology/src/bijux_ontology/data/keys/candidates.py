"""Conservative component discrimination before authoritative key comparison."""

from collections.abc import Iterator

from rdflib import BNode, Literal

from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.execution.limits import Budget

from .extraction import _Key

Token = tuple[str | None, ...]


class KeyCandidates:
    """Exclude only keys with a proven different component, retaining input order.

    Semantic literals share a partition: promotion can be non-transitive and
    unsupported values must remain candidates. Blank endpoints are wildcards
    because equal labels from different snapshots do not establish inequality.
    """

    def __init__(self, contract: KeyContract, budget: Budget) -> None:
        self.contract = contract
        self.budget = budget
        budget.consume("states", 3 * len(contract.components))
        budget.reserve_storage(256 + 256 * len(contract.components))
        self._buckets: list[dict[Token, list[int]]] = [{} for _ in contract.components]
        self._blanks: list[list[int]] = [[] for _ in contract.components]
        self._keys: list[_Key] = []

    def _token(self, key: _Key, position: int) -> Token | None:
        value = key.values[position]
        self.budget.consume("work")
        if isinstance(value, BNode):
            return None
        if self.contract.components[position].equality != "rdf-term" and isinstance(
            value, Literal
        ):
            return ("semantic-literal",)
        token = key.tokens[position]
        self.budget.consume("work", 1 + sum(len(part or "") for part in token))
        return token

    def add(self, key: _Key) -> None:
        """Fund retained component references before extending the index."""
        position = len(self._keys)
        for index in range(len(self.contract.components)):
            token = self._token(key, index)
            self.budget.consume("states", 2)
            self.budget.reserve_storage(128)
            if token is None:
                self._blanks[index].append(position)
            else:
                self._buckets[index].setdefault(token, []).append(position)
        self.budget.consume("states")
        self.budget.reserve_storage(16)
        self._keys.append(key)

    def select(self, key: _Key) -> Iterator[_Key]:
        """Visit the smallest sound component neighbourhood in original order."""
        selected: tuple[list[int], list[int]] | None = None
        size = len(self._keys)
        for position in range(len(self.contract.components)):
            token = self._token(key, position)
            if token is None:
                continue
            bucket = self._buckets[position].get(token, [])
            blanks = self._blanks[position]
            if len(bucket) + len(blanks) < size:
                size = len(bucket) + len(blanks)
                selected = bucket, blanks
        if selected is None:
            yield from self._keys
            return
        # Both lists are already ordered and disjoint; do not allocate a sort
        # buffer proportional to the entire population for a selective lookup.
        bucket, blanks = selected
        a = b = 0
        while a < len(bucket) or b < len(blanks):
            self.budget.consume("work")
            if b == len(blanks) or (a < len(bucket) and bucket[a] < blanks[b]):
                yield self._keys[bucket[a]]
                a += 1
            else:
                yield self._keys[blanks[b]]
                b += 1
