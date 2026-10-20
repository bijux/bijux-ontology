"""Bounded ordinary JSON snapshots shared by patches, queries and reproductions."""

import json

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.values import owned_json


def serialized(value: object, budget: Budget) -> str:
    """Reserve conservative escaped JSON storage before formatting owned values.

    The existing ordinary-JSON measure counts every node and scalar character.
    Thirty-two bytes per unit covers escaping, separators and finite float text.
    Sorted object names give a conservative source snapshot comparison, keeping
    bool, integer and floating spellings distinct without claiming RDF equality.
    """
    owned, cost = owned_json(value, budget)
    size = 32 * (cost + 1)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("JSON snapshot representation exceeds max_bytes")
    budget.consume("bytes", size)
    return json.dumps(owned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot(value: object, budget: Budget) -> tuple[object, str]:
    """Own an ordinary JSON tree, eliminating caller container aliases."""
    text = serialized(value, budget)
    budget.consume("work", len(text) + 1)
    budget.consume("bytes", len(text.encode("utf-8")))
    return json.loads(text), text
