"""Bounded equality classes with original-premise paths, without RDF saturation."""

from collections import deque
from collections.abc import Iterable

from bijux_ontology.execution.limits import Budget

from .types import EqualityConsequence, EqualityRule, Statement, TermKey


def retain_premises(
    values: Iterable[Statement], budget: Budget
) -> tuple[Statement, ...]:
    """Deduplicate already qualified premises while charging retained proof work."""
    result: dict[Statement, None] = {}
    for statement in values:
        budget.consume(
            "work", 1 + sum(len(part or "") for term in statement for part in term)
        )
        if statement not in result:
            budget.consume("states")
            budget.consume("bytes", 64)
            result[statement] = None
    return tuple(result)


class Identity:
    """Finite union/find beside an independently retained equality proof graph.

    Only explicit identity and the selected characteristic/key implications add
    edges. Path compression changes no asserted fact and discards no premise.
    """

    def __init__(self, budget: Budget) -> None:
        """Own fresh operation-local classes and bounded original evidence."""
        self.budget = budget
        self.parents: dict[TermKey, TermKey] = {}
        self.ranks: dict[TermKey, int] = {}
        self.edges: dict[TermKey, list[tuple[TermKey, tuple[Statement, ...]]]] = {}
        self.consequences: list[EqualityConsequence] = []
        self.merges: list[tuple[TermKey, TermKey]] = []

    def find(self, value: TermKey) -> TermKey:
        """Find an established class, retaining no correspondence across sources."""
        self.budget.consume("work", 1 + sum(len(part or "") for part in value))
        if value not in self.parents:
            self.budget.consume("states", 3)
            self.budget.consume("bytes", 160)
            self.parents[value] = value
            self.ranks[value] = 0
            self.edges[value] = []
            return value
        root = value
        while self.parents[root] != root:
            self.budget.consume("work")
            root = self.parents[root]
        while self.parents[value] != value:
            self.budget.consume("work")
            previous = self.parents[value]
            self.parents[value] = root
            value = previous
        return root

    def merge(
        self,
        left: TermKey,
        right: TermKey,
        rule: EqualityRule,
        premises: Iterable[Statement],
    ) -> bool:
        """Establish one equality only after reserving its complete finite proof."""
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        proof = retain_premises(premises, self.budget)
        self.budget.consume("states", 3)
        self.budget.consume("bytes", 192)
        if self.ranks[a] < self.ranks[b]:
            a, b = b, a
        self.budget.consume("states")
        self.budget.reserve_storage(64)
        self.merges.append((a, b))
        self.parents[b] = a
        if self.ranks[a] == self.ranks[b]:
            self.ranks[a] += 1
        self.edges[left].append((right, proof))
        self.edges[right].append((left, proof))
        self.consequences.append(EqualityConsequence(left, right, rule, proof))
        return True

    def proof(self, left: TermKey, right: TermKey) -> tuple[Statement, ...] | None:
        """Return actual equality premises, or absence of an established identity."""
        if self.find(left) != self.find(right):
            return None
        if left == right:
            return ()
        pending = deque([left])
        previous: dict[TermKey, tuple[TermKey, tuple[Statement, ...]] | None] = {
            left: None
        }
        self.budget.consume("states")
        while pending:
            self.budget.consume("work")
            current = pending.popleft()
            for neighbour, premises in self.edges[current]:
                self.budget.consume("work")
                if neighbour in previous:
                    continue
                self.budget.consume("states")
                self.budget.consume("bytes", 64)
                previous[neighbour] = current, premises
                if neighbour == right:
                    result: list[Statement] = []
                    cursor = right
                    while (step := previous[cursor]) is not None:
                        self.budget.consume("work")
                        cursor, used = step
                        self.budget.consume("states", len(used))
                        result.extend(used)
                    return retain_premises(result, self.budget)
                pending.append(neighbour)
        raise AssertionError("Established equality has no retained proof path")
