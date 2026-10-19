"""Immutable finite context definitions, scope requests and comparison evidence."""

import json
from dataclasses import dataclass
from typing import Literal

from bijux_ontology.absence import Absent
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, bound_text


@dataclass(frozen=True)
class ContextScope:
    """Apply one selected scoped definition at an explicitly declared parent.

    This is a context transition, not an assertion that every future object
    uses this scope. Property/type transitions retain their different processor
    protection and propagation behavior.
    """

    term: str
    kind: Literal["property", "type"] = "property"

    def __post_init__(self) -> None:
        """Require finite term and transition intent without interpreting a context."""
        budget = Budget()
        bound_text(self.term, budget)
        bound_text(self.kind, budget)
        if not self.term or self.kind not in ("property", "type"):
            raise InputError("Select a named property or type scoped context")


@dataclass(frozen=True)
class TermDefinition:
    """Processed mapping and defaults; absent and explicit null stay distinct."""

    term: str
    iri: str | None
    reverse: bool
    type_mapping: str | Absent
    containers: tuple[str, ...]
    index: str | Absent
    index_type: str | None | Absent
    index_language: str | None | Absent
    index_direction: str | None | Absent
    language: str | None | Absent
    direction: str | None | Absent
    effective_language: str | None
    effective_direction: str | None
    prefix: bool
    protected: bool
    nest: str | Absent
    scoped_context_json: str | None


@dataclass(frozen=True)
class ContextDefaults:
    """Effective defaults and the actual document-relative base policy."""

    processing_mode: str
    document_base: str | None
    base_mapping: str | None | Absent
    effective_base: str | None
    vocabulary: str | None
    language: str | None
    direction: str | None


@dataclass(frozen=True)
class ContextView:
    """Owned context state with finite, explicit previous-context reversion."""

    defaults: ContextDefaults
    terms: tuple[TermDefinition, ...]
    previous: "ContextView | None" = None


@dataclass(frozen=True)
class ContextInspection:
    """Root and requested transitions; unknown/invalid scopes never become views."""

    view: ContextView | None
    scopes: tuple[tuple[ContextScope, ContextView | None], ...]
    report: Report
    data_json: str

    @property
    def data(self) -> object:
        """Export fresh JSON transport data without exposing processor objects."""
        return json.loads(self.data_json)


@dataclass(frozen=True)
class ContextChange:
    """One effective facet change, preserving separately owned before/after data."""

    scope: str
    term: str | None
    facet: str
    before_json: str
    after_json: str

    @property
    def before(self) -> object:
        """Inspect an independent representation of the previous definition facet."""
        return json.loads(self.before_json)

    @property
    def after(self) -> object:
        """Inspect an independent representation of the replacement definition facet."""
        return json.loads(self.after_json)


@dataclass(frozen=True)
class ContextComparison:
    """Finite processed-definition differences, never all-document compatibility."""

    before: ContextInspection
    after: ContextInspection
    changes: tuple[ContextChange, ...]
    report: Report
    data_json: str

    @property
    def data(self) -> object:
        """Export the already bounded comparison as independent JSON data."""
        return json.loads(self.data_json)
