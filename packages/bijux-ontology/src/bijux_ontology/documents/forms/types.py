"""Explicit source-occurrence representation contracts and independent reports."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.selectors.types import EntitySelector, SourceSelector
from bijux_ontology.limits import Budget
from bijux_ontology.reports import Report


def _keys(values: Sequence[str], budget: Budget) -> tuple[str, ...]:
    """Capture finite exact key spellings, without context or keyword inference."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Form keys require a finite sequence of strings")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count)
    captured: list[str] = []
    for index in range(count):
        budget.consume("work")
        try:
            key = values[index]
        except IndexError as error:
            raise InputError("Form keys changed during capture") from error
        if not isinstance(key, str):
            raise InputError("Form keys must be strings")
        if len(key) > budget.limits.max_bytes:
            raise LimitExceeded("Form key exceeds max_bytes")
        budget.consume("work", len(key))
        try:
            budget.consume("bytes", len(key.encode("utf-8")))
        except UnicodeEncodeError as error:
            raise InputError("Form keys require valid Unicode") from error
        captured.append(key)
    return tuple(captured)


@dataclass(frozen=True)
class FormRequirement:
    """Check only the chosen raw source occurrences, never joined entity facts.

    Shape and key spelling do not imply RDF meaning. ``reference`` explicitly
    requires an object containing only canonical ``@id`` with a string value.
    ``inline`` requires an established describing resource from this occurrence.
    Keys remain unrestricted unless ``allowed_keys`` is explicitly supplied.
    """

    selector: SourceSelector | EntitySelector
    shape: Literal["object", "array", "scalar"] | None = None
    required_keys: tuple[str, ...] = ()
    allowed_keys: tuple[str, ...] | None = None
    embedding: Literal["reference", "inline"] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selector, SourceSelector | EntitySelector):
            raise InputError("Form requirements need a typed source selector")
        if (
            isinstance(self.selector, EntitySelector)
            and self.selector.coverage_unit != "source_occurrence"
        ):
            raise InputError("Form checking requires source-occurrence coverage")
        if self.shape not in (None, "object", "array", "scalar"):
            raise InputError("Unknown source form shape")
        if self.embedding not in (None, "reference", "inline"):
            raise InputError("Unknown embedding form")
        budget = Budget()
        required = _keys(self.required_keys, budget)
        allowed = (
            None if self.allowed_keys is None else _keys(self.allowed_keys, budget)
        )
        if self.shape in ("array", "scalar") and (
            required or allowed is not None or self.embedding is not None
        ):
            raise InputError("Key and embedding requirements apply to objects")
        if allowed is not None and not set(required).issubset(allowed):
            raise InputError("Required form keys must also be allowed")
        object.__setattr__(self, "required_keys", required)
        object.__setattr__(self, "allowed_keys", allowed)


@dataclass(frozen=True)
class DocumentFormCheck:
    """Raw form and semantic truth remain separate from their required aggregate."""

    form_report: Report
    semantic_report: Report
    report: Report
