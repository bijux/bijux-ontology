"""Exact JSON scalar fidelity shared by optional R5 terminology resources."""

from collections.abc import Callable

from bijux_ontology._json import _decode_json
from bijux_ontology.execution.limits import Budget


class UnsupportedJsonNumber(Exception):
    """A valid numeric token exceeds the qualified exact native profile."""


def decode_fhir_json(
    payload: bytes | str,
    budget: Budget,
    *,
    key_hook: Callable[[str], None] | None = None,
) -> object:
    """Decode original integers exactly; fractional native fidelity is unqualified."""

    def qualify_number(
        token: str, _path: tuple[str | int, ...], integer: bool
    ) -> object:
        """Reject fractional tokens before any binary float can lose their spelling."""
        if not integer:
            raise UnsupportedJsonNumber(
                "Fractional JSON numbers are outside the exact supported profile"
            )
        budget.consume("work", len(token) + 1)
        budget.consume("bytes", len(token))
        return int(token)

    return _decode_json(
        payload,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
        key_hook=key_hook,
        number_hook=qualify_number,
        budget=budget,
    )


def same_json(left: object, right: object, budget: Budget) -> bool:
    """Compare supported source scalars exactly, including bool versus integer."""
    pending = [(left, right)]
    while pending:
        budget.consume("work")
        first, second = pending.pop()
        if type(first) is not type(second):
            return False
        if isinstance(first, dict) and isinstance(second, dict):
            if len(first) != len(second):
                return False
            budget.consume("states", len(first))
            for key, value in first.items():
                budget.consume("work", len(key) + 1)
                if key not in second:
                    return False
                pending.append((value, second[key]))
        elif isinstance(first, list) and isinstance(second, list):
            if len(first) != len(second):
                return False
            budget.consume("states", len(first))
            pending.extend(zip(first, second, strict=True))
        else:
            if isinstance(first, str):
                budget.consume("work", len(first) + 1)
            if first != second:
                return False
    return True
