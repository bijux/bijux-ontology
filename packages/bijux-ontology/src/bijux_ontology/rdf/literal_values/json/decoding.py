"""Exact numeric adaptation of the existing strict maintained JSON decoder."""

from decimal import Context, Decimal, DecimalException
from sys import getsizeof

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import decode_owned_json


class _Unavailable(InputError):
    """A syntactically valid request has no qualified finite numeric representation."""


def decimal_bound(value: Decimal, budget: Budget) -> None:
    """Bound supplied coefficient storage before tuple/string allocation."""
    if type(value) is not Decimal or not value.is_finite():
        raise InputError("JSON numeric bounds require finite Decimal values")
    size = getsizeof(value)
    # The accepted CPython Decimal representation uses packed coefficients;
    # reserve conservatively for unpacked decimal text before requesting it.
    reservation = 128 + 8 * size
    if reservation > budget.limits.max_bytes:
        raise LimitExceeded("JSON numeric formatting exceeds max_bytes")
    budget.consume("bytes", reservation)
    budget.consume("work", size)
    bound_text(str(value), budget)


def integral(value: Decimal) -> bool:
    """Test exact finite integrality without context-dependent arithmetic."""
    return value == value.to_integral_value(context=Context())


def decode_exact(text: str, budget: Budget) -> object:
    """Retain exact numbers while the shared decoder owns all JSON syntax.

    The existing numeric callback temporarily returns a bounded integer slot;
    every original JSON number uses that callback. A second bounded ordinary
    container walk replaces those slots with immutable Decimal values. No JSON
    grammar, RDF term, or new semantic tree representation is introduced.
    """
    bound_text(text, budget)
    numbers: list[Decimal] = []

    def number(token: str, path: tuple[str | int, ...], integer: bool) -> int:
        """Reserve each original coefficient before maintained exact conversion."""
        bound_text(token, budget)
        budget.consume("states", len(token) + 1)
        budget.consume("bytes", 64 + 8 * len(token))
        try:
            value = Decimal(token, context=Context())
        except DecimalException as error:
            raise _Unavailable("JSON numeric magnitude is not supported") from error
        if not value.is_finite():
            raise _Unavailable("JSON number has no finite exact representation")
        decimal_bound(value, budget)
        index = len(numbers)
        numbers.append(value)
        return index

    intermediate = decode_owned_json(
        text,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
        number_hook=number,
        key_hook=lambda key: bound_text(key, budget),
        budget=budget,
    ).value

    def restore(value: object, depth: int = 0) -> object:
        """Own ordinary containers with each final numeric value restored once."""
        budget.consume("work")
        budget.consume("states")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Decoded JSON exceeds max_depth")
        if type(value) is int:
            return numbers[value]
        if isinstance(value, dict):
            budget.consume("bytes", 64 + 32 * len(value))
            return {key: restore(child, depth + 1) for key, child in value.items()}
        if isinstance(value, list):
            budget.consume("bytes", 64 + 8 * len(value))
            return [restore(child, depth + 1) for child in value]
        return value

    return restore(intermediate)


def equal_values(left: object, right: object, budget: Budget, depth: int = 0) -> bool:
    """Compare exact parsed JSON values without RDF equality or boolean coercion."""
    budget.consume("work")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("JSON value comparison exceeds max_depth")
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        decimal_bound(left, budget)
        decimal_bound(right, budget)
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, str) and isinstance(right, str):
        bound_text(left, budget)
        bound_text(right, budget)
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            equal_values(a, b, budget, depth + 1)
            for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        if len(left) != len(right):
            return False
        for key, value in left.items():
            bound_text(key, budget)
            if key not in right or not equal_values(
                value, right[key], budget, depth + 1
            ):
                return False
        return True
    return left == right
