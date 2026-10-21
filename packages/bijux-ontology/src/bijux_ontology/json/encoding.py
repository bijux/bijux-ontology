"""Bounded canonical JSON bytes for inert identities and persistence records."""

import json

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget


def json_bytes(value: object, budget: Budget) -> bytes:
    """Reserve JSON traversal and worst-case encoding space before allocation."""
    budget.consume("states")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        budget.consume("work")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Preparation JSON exceeds max_depth")
        if isinstance(item, str):
            if len(item) > budget.limits.max_bytes:
                raise LimitExceeded("Preparation text exceeds max_bytes")
            budget.consume("bytes", 2 + 6 * len(item))
        elif isinstance(item, dict):
            budget.consume("states", 2 * len(item))
            budget.consume("bytes", 2 + 128 * len(item))
            pending.extend(
                (child, depth + 1) for pair in item.items() for child in pair
            )
        elif isinstance(item, (list, tuple)):
            budget.consume("states", len(item))
            budget.consume("bytes", 2 + 64 * len(item))
            pending.extend((child, depth + 1) for child in item)
        elif item is None or type(item) in (bool, int, float):
            if type(item) is int and item.bit_length() > min(
                4096, budget.limits.max_bytes
            ):
                raise LimitExceeded("Preparation integer exceeds max_bytes")
            budget.consume(
                "bytes", 32 if type(item) is not int else 32 + item.bit_length()
            )
        else:
            raise InputError("Preparation requires inert JSON data")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InputError("Preparation requires finite valid JSON") from error
    if len(payload) > budget.limits.max_bytes:
        raise LimitExceeded("Preparation artifact exceeds max_bytes")
    return payload
