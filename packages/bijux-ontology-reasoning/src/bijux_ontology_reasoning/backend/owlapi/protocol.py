"""Bounded OWLAPI worker scalar and inventory adoption without process ownership."""

from typing import cast

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.limits import Budget, bound_text


def result_counter(value: object, budget: Budget, maximum: int) -> int:
    """Qualify native counters before formatting or exposing retained metadata."""
    if type(value) is not int or value < 0 or value > maximum:
        raise ResourceError("Invalid OWL profile result counter")
    budget.consume("work")
    return value


def result_text(value: object, budget: Budget, *, optional: bool = False) -> str | None:
    """Bound typed worker text before diagnostic or result retention."""
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ResourceError("Invalid OWL profile result text")
    bound_text(value, budget)
    return value


def result_records(raw: object, budget: Budget) -> list[object]:
    """Prepay native inventory iteration and retained references."""
    if type(raw) is not list:
        raise ResourceError("Diagnosis omitted a required finite inventory")
    budget.consume("work", len(raw))
    budget.consume("states", len(raw))
    budget.consume("bytes", 64 + 16 * len(raw))
    return cast(list[object], raw)


def result_boolean(raw: object) -> bool:
    """Preserve exact native booleans without accepting integer substitutes."""
    if type(raw) is not bool:
        raise ResourceError("Diagnosis omitted a required boolean")
    return raw
