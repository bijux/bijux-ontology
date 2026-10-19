"""Bounded capture of finite typed operation inputs."""

from collections.abc import Sequence
from typing import TypeVar

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget

_T = TypeVar("_T")


def capture_items(
    values: Sequence[_T], expected: type[_T], budget: Budget
) -> tuple[_T, ...]:
    """Read one cached finite sequence length before bounded indexed capture."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Input inventories require finite typed sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError("Input inventory changed during capture") from error
        if not isinstance(value, expected):
            raise InputError("Input inventory contains an invalid record")
        result.append(value)
    return tuple(result)
