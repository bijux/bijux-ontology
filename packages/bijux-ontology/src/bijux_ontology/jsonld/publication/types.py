"""Opt-in publication promises for selected root definitions and real probes."""

import json
from dataclasses import dataclass
from typing import Literal

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.jsonld.composition.types import ContextCase
from bijux_ontology.jsonld.contexts.types import ContextInspection
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class PublishedTerm:
    """Require effective root protection or intentional root extensibility."""

    term: str
    protected: bool


@dataclass(frozen=True)
class ContextOverride:
    """Exercise one selected term's replacement with maintained processing.

    A permitted replacement requires representative extension cases. Blocked
    replacements must be independently valid and actually rejected by the
    protected-definition guard, rather than an arbitrary malformed-input error.
    """

    name: str
    term: str
    definition: object
    expect: Literal["allowed", "blocked"]
    cases: tuple[ContextCase, ...] = ()


@dataclass(frozen=True)
class PublicationContract:
    """Explicit root policies; no requirements apply to ordinary JSON-LD APIs.

    required_version selects an authored LOCAL root @version declaration. It is
    not inferred from implicit processing mode or unavailable import history.
    ABSENT leaves vocabulary/effective base unconstrained; None requires absence.
    Every selected term needs its actual expected override scenario.
    """

    terms: tuple[PublishedTerm, ...]
    cases: tuple[ContextCase, ...]
    overrides: tuple[ContextOverride, ...]
    required_version: Literal["1.1"] | None = None
    vocabulary: str | None | Absent = ABSENT
    effective_base: str | None | Absent = ABSENT


@dataclass(frozen=True)
class ContextOverrideResult:
    """Policy truth beside unchanged raw native replacement/override reports.

    Expected native rejection stays an input error on inspection.report; the
    separate policy report says whether that precisely requested guard worked.
    """

    name: str
    replacement: ContextInspection | None = None
    inspection: ContextInspection | None = None
    cases: tuple[Report, ...] = ()
    report: Report | None = None


@dataclass(frozen=True)
class ContextPublication:
    """Owned original input, retained obligations, and explicit publication truth."""

    context_json: str | None
    inspection: ContextInspection | None
    cases: tuple[Report, ...]
    overrides: tuple[ContextOverrideResult, ...]
    report: Report

    @property
    def context(self) -> object:
        """Inspect independent original JSON; only report.verified is acceptance."""
        return json.loads(self.context_json) if self.context_json is not None else None
