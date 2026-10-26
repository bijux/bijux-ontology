"""Bounded exact row identity when answers need no blank correspondence."""

from collections.abc import Iterator
from itertools import zip_longest

from rdflib import BNode

from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import AnswerArtifact, AnswerSemantics

_Row = tuple[tuple[str | None, ...] | None, ...]


def _tokens(
    artifact: AnswerArtifact, positions: tuple[int, ...], budget: Budget
) -> Iterator[_Row]:
    """Fund exact lexical keys and retained row references before allocation."""
    for row in artifact.rows:
        budget.consume("work", len(row) + 1)
        budget.consume("states", len(row) + 1)
        budget.reserve_storage(128 * (len(row) + 1))
        for cell in row:
            if cell is not None:
                bound_node(cell, budget)
                budget.reserve_storage(
                    8
                    * (
                        len(cell)
                        + len(getattr(cell, "datatype", "") or "")
                        + len(getattr(cell, "language", "") or "")
                        + 64
                    )
                )
        yield tuple(
            term_key(row[index]) if row[index] is not None else None
            for index in positions
        )


def compare_ground_answers(
    left: AnswerArtifact,
    right: AnswerArtifact,
    semantics: AnswerSemantics,
    budget: Budget,
) -> bool | None:
    """Return exact ground identity, or None to require global blank matching.

    Only original input blanks select RDF isomorphism. Unbound cells and empty
    rows remain distinct row tokens; bag counts never become set membership.
    Both inputs are fully qualified even after an early ordered disagreement.
    """
    for artifact in (left, right):
        for row in artifact.rows:
            budget.consume("work")
            for cell in row:
                budget.consume("work")
                if isinstance(cell, BNode):
                    return None
    width = len(left.variables)
    budget.consume("states", 3 * width + 1)
    budget.reserve_storage(128 * (width + 1))
    budget.consume("work", 3 * width + 1)
    indices = {name: index for index, name in enumerate(right.variables)}
    before = _tokens(left, tuple(range(width)), budget)
    after = _tokens(right, tuple(indices[name] for name in left.variables), budget)
    if semantics == "sequence":
        equal = True
        for a, b in zip_longest(before, after):
            budget.consume("work")
            if a != b:
                equal = False
    else:
        counts: list[dict[_Row, int]] = []
        for tokens in (before, after):
            inventory: dict[_Row, int] = {}
            for key in tokens:
                budget.consume("work")
                inventory[key] = inventory.get(key, 0) + 1 if semantics == "bag" else 1
            counts.append(inventory)
        budget.consume("work", len(counts[0]) + len(counts[1]))
        equal = counts[0] == counts[1]
    budget.consume("work")
    return equal
