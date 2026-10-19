"""JSON type and array equality over already owned ordinary values."""

from __future__ import annotations

from typing import cast

from bijux_ontology.execution.limits import Budget


def same_json(actual: object, expected: object, budget: Budget) -> bool:
    """Compare owned JSON without numeric coercion or changing array order."""
    budget.consume("work")
    if isinstance(actual, dict):
        if not isinstance(expected, dict):
            return False
        other = cast(dict[str, object], expected)
        if len(actual) != len(other):
            return False
        budget.reserve_storage(256)
        for key, value in actual.items():
            budget.consume("work", len(key) + 1)
            if key not in other or not same_json(value, other[key], budget):
                return False
        return True
    if isinstance(actual, list):
        if not isinstance(expected, list):
            return False
        items = cast(list[object], expected)
        if len(actual) != len(items):
            return False
        budget.reserve_storage(256)
        return all(
            same_json(value, items[index], budget) for index, value in enumerate(actual)
        )
    if isinstance(actual, str):
        if not isinstance(expected, str):
            return False
        budget.consume("work", len(actual) + len(expected) + 1)
        return bool(str.__eq__(actual, expected))
    if type(actual) is not type(expected):
        return False
    if type(actual) is int:
        budget.consume("work", actual.bit_length() // 8 + 1)
    return bool(actual == expected)
