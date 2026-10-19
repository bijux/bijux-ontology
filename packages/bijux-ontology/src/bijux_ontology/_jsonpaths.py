"""Concrete JSON source paths and bounded, escaped diagnostic pointers."""

from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, _bound_text_size

SourcePath = tuple[str | int, ...]


def location(path: SourcePath) -> str:
    """Render the selected input occurrence as an escaped JSON Pointer."""
    return "/" + "/".join(
        str(key).replace("~", "~0").replace("/", "~1") for key in path
    )


def at_path(document: object, path: SourcePath) -> object:
    """Follow concrete keys and nonnegative array indexes."""
    current = document
    for key in path:
        if isinstance(key, str) and isinstance(current, dict):
            current = cast(dict[str, object], current)[key]
        elif type(key) is int and key >= 0 and isinstance(current, list):
            current = cast(list[object], current)[key]
        else:
            raise KeyError(key)
    return current


def validate_path(path: SourcePath) -> None:
    """Require concrete string keys and nonnegative array indices."""
    if not isinstance(path, tuple) or any(
        type(key) not in (str, int) or isinstance(key, int) and key < 0 for key in path
    ):
        raise InputError("Expected a concrete key/index path")


def bound_pointer_text(value: str, budget: Budget) -> int:
    """Bound text and reserve its escaped pointer storage before diagnostics."""
    size = _bound_text_size(value, budget)
    escaped = size + value.count("~") + value.count("/")
    if escaped > budget.limits.max_bytes:
        raise LimitExceeded("Selector text exceeds max_bytes")
    budget.consume("bytes", 64 + 2 * escaped)
    return escaped


def bound_index(value: int, budget: Budget) -> int:
    """Bound decimal indexes/counts before Python or diagnostics format them."""
    digits = max(1, (value.bit_length() * 30103) // 100000 + 1)
    if digits > budget.limits.max_bytes:
        raise LimitExceeded("Selector number exceeds max_bytes")
    budget.consume("work", digits)
    budget.consume("bytes", 64 + digits)
    try:
        text = str(value)
    except ValueError as error:
        raise LimitExceeded("Selector number exceeds runtime text bounds") from error
    return bound_pointer_text(text, budget)


def bound_path(path: SourcePath, budget: Budget) -> None:
    """Charge finite path components and pointer bounds before source lookup."""
    if not isinstance(path, tuple):
        raise InputError("Expected a concrete key/index path")
    if len(path) > budget.limits.max_states:
        raise LimitExceeded("Selector path exceeds max_states")
    budget.consume("work", len(path))
    budget.consume("bytes", 64 + 16 * len(path))
    validate_path(path)
    size = 1
    for part in path:
        size += (
            bound_index(part, budget)
            if isinstance(part, int)
            else bound_pointer_text(part, budget)
        ) + 1
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Selector pointer exceeds max_bytes")
    budget.consume("work", size)
    budget.consume("bytes", 64 + size)


def bounded_location(path: SourcePath, budget: Budget) -> str:
    """Render diagnostic text only after charging its actual source path."""
    bound_path(path, budget)
    return location(path)
