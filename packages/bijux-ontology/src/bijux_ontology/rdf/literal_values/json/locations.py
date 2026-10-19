"""Bounded original-literal locations for nested JSON value evidence."""

from bijux_ontology._jsonpaths import bounded_location
from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Location
from bijux_ontology.limits import Budget


def value_location(
    path: tuple[str | int, ...], anchor: str, budget: Budget
) -> tuple[str, Location]:
    """Bound an aggregate UTF-8 JSON pointer before joining its source anchor."""
    pointer = bounded_location(path, budget) if path else ""
    size = len(anchor.encode("utf-8")) + 1 + len(pointer.encode("utf-8"))
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Literal JSON location exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", len(anchor) + len(pointer) + 1)
    source = anchor + "#"
    return source + pointer, Location(source=source, path=path)
