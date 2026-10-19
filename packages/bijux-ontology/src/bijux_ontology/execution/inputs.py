"""Bounded capture of finite typed operation inputs."""

from collections import deque
from collections.abc import Sequence
from typing import Any, TypeVar

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Budget

_T = TypeVar("_T")


def capture(
    values: Sequence[Any] | deque[Any],
    budget: Budget,
    *,
    expected: type[Any] | None = None,
) -> tuple[Any, ...]:
    """Own one finite indexed inventory, optionally checking each original record."""
    if not isinstance(values, Sequence | deque) or isinstance(values, str | bytes):
        raise InputError("Output contracts require finite sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError(
                "Input inventory changed during capture"
                if expected is not None
                else "Output inventory changed during capture"
            ) from error
        if expected is not None and not isinstance(value, expected):
            raise InputError("Input inventory contains an invalid record")
        result.append(value)
    return tuple(result)


def capture_items(
    values: Sequence[_T], expected: type[_T], budget: Budget
) -> tuple[_T, ...]:
    """Retain typed sequence admission with the shared indexed capture engine."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Input inventories require finite typed sequences")
    return capture(values, budget, expected=expected)
