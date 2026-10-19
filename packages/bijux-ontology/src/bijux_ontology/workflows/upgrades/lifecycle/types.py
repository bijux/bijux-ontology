"""Explicit supplied release intervals and observable identifier lifecycle evidence."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.roles.types import TermPosition
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.vocabulary.browsing.results import Deprecation, TermInfo
from bijux_ontology.vocabulary.comparison import VocabularyChanges
from bijux_ontology.vocabulary.facts.types import FactPredicate, SelectedFact


@dataclass(frozen=True)
class Release:
    """A caller-ordered snapshot label; labels do not imply version ordering."""

    label: str
    source: PreparedSource
    imports: Mapping[str, RdfSource | Payload] | None = None

    def __post_init__(self) -> None:
        """Require owned sources without copying or guessing their physical scope."""
        if type(self.label) is not str or not self.label:
            raise InputError("A release requires an explicit nonempty text label")
        if not isinstance(self.source, PreparedSource):
            raise InputError("A release requires a PreparedSource")
        if self.imports is not None and not isinstance(self.imports, Mapping):
            raise InputError("Release imports require explicit local resource bindings")


@dataclass(frozen=True)
class Tombstone:
    """Caller-supplied explicit retirement at one supplied release label."""

    term: URIRef
    release: str

    def __post_init__(self) -> None:
        """Keep RDF identity separate from descriptive release text."""
        if not isinstance(self.term, URIRef) or type(self.release) is not str:
            raise InputError("A tombstone requires an IRI and exact release label")


@dataclass(frozen=True)
class HistoryCoverage:
    """A declared complete supplied interval, never permanent historical stability."""

    labels: tuple[str, ...]
    complete: bool

    def __post_init__(self) -> None:
        """Require an immutable finite coverage declaration."""
        if type(self.labels) is not tuple or type(self.complete) is not bool:
            raise InputError("Coverage requires a label tuple and explicit boolean")
        if any(type(label) is not str or not label for label in self.labels):
            raise InputError("History labels require nonempty text")


@dataclass(frozen=True)
class LifecyclePolicy:
    """Consumer decisions over explicit retirement and selected original positions.

    Reserved predicates have caller-declared logical meaning. An empty inventory
    imposes no structural reservation. Disappearance-to-declaration counts as
    reactivation only when selected and the supplied interval is complete.
    """

    reactivation: Literal["allow", "forbid"] = "forbid"
    reserved: tuple[FactPredicate, ...] = ()
    positions: tuple[TermPosition, ...] = ("subject", "object")
    forbid_retired_assertions: bool = True
    declaration_reactivation: bool = False

    def __post_init__(self) -> None:
        """Reject permissive fallback from malformed policy declarations."""
        if self.reactivation not in ("allow", "forbid"):
            raise InputError("Select an explicit reactivation policy")
        if type(self.reserved) is not tuple or any(
            not isinstance(rule, FactPredicate) for rule in self.reserved
        ):
            raise InputError(
                "Reserved predicates require immutable FactPredicate rules"
            )
        if (
            type(self.positions) is not tuple
            or not self.positions
            or set(self.positions)
            - {"subject", "predicate", "object", "datatype", "graph"}
        ):
            raise InputError("Select explicit reserved identifier positions")
        if any(
            type(value) is not bool
            for value in (self.forbid_retired_assertions, self.declaration_reactivation)
        ):
            raise InputError("Lifecycle policy switches require explicit booleans")


@dataclass(frozen=True)
class LifecycleObservation:
    """Exact raw retirement and selected fact evidence for one term and snapshot."""

    release: Release
    term: URIRef
    declaration: TermInfo
    deprecation: Deprecation
    tombstone: Tombstone | None
    reserved_facts: tuple[SelectedFact, ...]


@dataclass(frozen=True)
class LifecycleEvent:
    """An observed transition or policy finding, without semantic intention claims."""

    kind: Literal["retired", "reactivated", "reserved_assertion"]
    term: URIRef
    release: str
    retirement: str
    prohibited: bool
    fact: SelectedFact | None = None


@dataclass(frozen=True)
class LifecycleCheck:
    """Finite supplied history evidence and ordinary adjacent editorial differences."""

    observations: tuple[LifecycleObservation, ...]
    events: tuple[LifecycleEvent, ...]
    changes: tuple[VocabularyChanges, ...]
    coverage: HistoryCoverage | None
    report: Report
