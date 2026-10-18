"""Explicit ordered context modules and retained representative obligations."""

import json
from dataclasses import dataclass

from bijux_ontology.jsonld.contexts.types import ContextInspection
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class ContextCase:
    """One required representative object without its own root @context.

    Nested contexts remain part of the actual sample and are processed normally.
    Expectations describe required exercised behavior, not all future documents.
    Composition captures every input before any supplied loader is called.
    """

    name: str
    document: object
    expectations: tuple[ContextExpectation, ...]


@dataclass(frozen=True)
class ContextModule:
    """A context value with nonempty required cases, selected in caller order."""

    name: str
    context: object
    cases: tuple[ContextCase, ...]


@dataclass(frozen=True)
class ContextCaseResult:
    """Raw independent and composite checks of the same retained sample."""

    module: str
    case: str
    independent: Report | None = None
    composite: Report | None = None


@dataclass(frozen=True)
class ContextComposition:
    """Qualified reusable context or explicit unavailable retained obligations."""

    context_json: str | None
    order: tuple[str, ...]
    cases: tuple[ContextCaseResult, ...]
    inspection: ContextInspection | None
    report: Report

    @property
    def context(self) -> object:
        """Return independent JSON of the checked ordered context value."""
        return json.loads(self.context_json) if self.context_json is not None else None
