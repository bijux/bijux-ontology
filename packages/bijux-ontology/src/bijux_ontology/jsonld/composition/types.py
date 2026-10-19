"""Explicit ordered context modules and retained representative obligations."""

import json
from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.jsonld.contexts.types import ContextInspection
from bijux_ontology.jsonld.contracts import cases as case_contracts


@dataclass(frozen=True)
class ContextModule:
    """A context value with nonempty required cases, selected in caller order."""

    name: str
    context: object
    cases: tuple[case_contracts.ContextCase, ...]


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
