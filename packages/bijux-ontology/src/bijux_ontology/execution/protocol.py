"""Prepaid bounds for the closed JSON worker protocol."""

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget


def json_bound(value: object, budget: Budget) -> int:
    """Reserve a conservative JSON encoding before allocating its byte payload."""
    budget.consume("work")
    if isinstance(value, str):
        budget.consume("work", len(value))
        return 2 + 12 * len(value)
    if value is None or type(value) is bool:
        return 5
    if type(value) is int:
        if value.bit_length() > min(14000, 3 * budget.limits.max_bytes):
            raise LimitExceeded("Worker integer metadata exceeds its byte bound")
        return 4 + value.bit_length()
    if isinstance(value, list | tuple):
        return 2 + sum(1 + json_bound(item, budget) for item in value)
    if isinstance(value, dict):
        return 2 + sum(
            2 + json_bound(key, budget) + json_bound(item, budget)
            for key, item in value.items()
        )
    raise InputError("Unexpected Worker protocol value")
