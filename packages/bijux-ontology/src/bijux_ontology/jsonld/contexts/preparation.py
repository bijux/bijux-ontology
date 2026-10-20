"""Bounded context inputs and immutable evidence encoding shared by operations."""

import json
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from math import isfinite
from typing import TypeVar

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.paths import bound_index
from bijux_ontology.json.values import own_json

T = TypeVar("T")


def capture_selections(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    """Capture indexed finite inventories without trusting an arbitrary iterator."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise InputError("Context selections require finite sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            result.append(values[index])
        except IndexError as error:
            raise InputError("Context selection changed during capture") from error
    return tuple(result)


def context_budget(limits: Limits | None, budget: Budget | None) -> Budget:
    """Keep explicit limit settings aligned with one cumulative operation."""
    if budget is not None and (
        not isinstance(budget, Budget) or limits is not None and limits != budget.limits
    ):
        raise InputError("Use a shared Budget with matching limits")
    return budget if budget is not None else Budget(limits)


def preflight_context_json(value: object, budget: Budget, depth: int = 0) -> int:
    """Charge JSON metadata before copies, hashing, or processor callbacks."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Context metadata exceeds max_depth")
    if isinstance(value, str):
        bound_text(value, budget)
        return 1
    if value is None or type(value) is bool:
        return 1
    if type(value) is int:
        bound_index(value, budget)
        return 1
    if type(value) is float and isfinite(value):
        bound_text(repr(value), budget)
        return 1
    if type(value) is list:
        return 1 + sum(
            preflight_context_json(item, budget, depth + 1)
            for item in capture_selections(value, budget)
        )
    if type(value) is dict:
        budget.consume("states", len(value))
        total = 1
        for key, child in value.items():
            bound_text(key, budget)
            total += preflight_context_json(child, budget, depth + 1)
        return total
    raise InputError("Contexts require finite JSON values")


def capture_context_json(value: object, budget: Budget) -> object:
    """Own qualified JSON without preserving mutable caller dictionaries."""
    preflight_context_json(value, budget)
    return own_json(
        value, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    )


def context_data(value: object, budget: Budget, depth: int = 0) -> object:
    """Encode only the finite immutable records, preserving explicit absence."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Context records exceed max_depth")
    if value is ABSENT:
        return {"absent": True}
    if is_dataclass(value) and not isinstance(value, type):
        names = fields(value)
        budget.consume("states", len(names))
        return {
            field.name: context_data(getattr(value, field.name), budget, depth + 1)
            for field in names
        }
    if isinstance(value, tuple):
        budget.consume("states", len(value))
        return [context_data(item, budget, depth + 1) for item in value]
    preflight_context_json(value, budget, depth)
    return value


def encode_context_json(value: object, budget: Budget) -> str:
    """Store bounded independent transport data using the existing JSON boundary."""
    owned = capture_context_json(value, budget)
    result = json.dumps(
        owned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    bound_text(result, budget)
    return result
