"""Explicit source-object regions and immutable term-use policy evidence."""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.paths import SourcePath
from bijux_ontology.jsonld.selectors.types import EntitySelector, SourceSelector
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report

UsageRole = Literal[
    "explicit",
    "implicit-vocabulary",
    "compact-iri",
    "full-iri",
    "keyword",
    "nullified",
    "unmapped",
    "opaque",
    "unresolved",
]


class UsageTermStatus(Protocol):
    """Read the finite existing vocabulary status without a reverse import."""

    @property
    def status(self) -> Literal["declared", "referenced", "absent"]:
        """Distinguish declaration, reference-only presence and absence."""
        ...


@runtime_checkable
class TermUsageVocabulary(Protocol):
    """Trusted read-only Python lookup, normally an owned VocabularyView.

    The operation bounds calls and returned metadata; it does not sandbox Python
    code or authorize remote access by a supplied implementation.
    """

    def lookup(self, iri: str) -> UsageTermStatus:
        """Look up one exact already expanded identifier in the supplied scope."""
        ...


@dataclass(frozen=True)
class TermUsageRegion:
    """Check direct keys of each actual selected source object.

    Separate regions express application and extension policies. Descendants are
    selected explicitly with existing finite traversal steps; RDF entities never
    substitute for their differently scoped source descriptions.
    """

    selector: SourceSelector | EntitySelector
    implicit: Literal["forbid", "allow"] = "forbid"
    allow_compact_iris: bool = True
    allow_full_iris: bool = True
    ignored_terms: tuple[str, ...] = ()
    membership: Literal["declared", "present"] | None = None

    def __post_init__(self) -> None:
        """Own finite policy strings and require source occurrence coverage."""
        if not isinstance(self.selector, SourceSelector | EntitySelector):
            raise InputError("Term usage requires a typed source selector")
        if (
            isinstance(self.selector, EntitySelector)
            and self.selector.coverage_unit != "source_occurrence"
        ):
            raise InputError("Term usage requires source occurrence coverage")
        if self.implicit not in ("forbid", "allow") or self.membership not in (
            None,
            "declared",
            "present",
        ):
            raise InputError("Select explicit implicit-term and membership policies")
        if (
            type(self.allow_compact_iris) is not bool
            or type(self.allow_full_iris) is not bool
        ):
            raise InputError("IRI permissions require booleans")
        if type(self.ignored_terms) is not tuple:
            raise InputError("Ignored terms require an immutable string tuple")
        budget = Budget()
        budget.consume("states", len(self.ignored_terms))
        owned = []
        for value in self.ignored_terms:
            bound_text(value, budget)
            owned.append(str(value))
        object.__setattr__(self, "ignored_terms", tuple(owned))


@dataclass(frozen=True)
class TermUsage:
    """One actual key role; acceptance concerns only the selected application rule."""

    path: SourcePath
    term: str
    expanded_iri: str | None
    role: UsageRole
    accepted: bool


@dataclass(frozen=True)
class TermUsageResult:
    """Bounded source-key evidence and retained selection/processing obligations."""

    usages: tuple[TermUsage, ...]
    report: Report
