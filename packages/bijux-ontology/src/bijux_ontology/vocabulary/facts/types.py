"""Explicit asserted-fact selections and their original RDF evidence."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.roles.types import (
    Statement,
    TermPosition,
    TermRole,
    TermRoleOccurrence,
)

_ROLES = frozenset(
    (
        "class",
        "concept",
        "individual",
        "property",
        "object_property",
        "datatype_property",
        "annotation_property",
        "datatype",
        "structural",
        "declaration",
        "unknown",
    )
)


def _own(values: object, expected: type[object], label: str) -> tuple[object, ...]:
    """Own a finite typed declaration inventory without accepting iterators."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError(f"{label} must be a finite typed sequence")
    count = len(values)
    if count > Limits().max_states:
        raise InputError(f"{label} exceeds the declaration bound")
    result = tuple(values[index] for index in range(count))
    if any(not isinstance(value, expected) for value in result):
        raise InputError(f"{label} contains an unsupported declaration")
    return tuple(str(value) for value in result) if expected is str else result


@dataclass(frozen=True)
class FactPredicate:
    """Select an exact predicate and optionally qualify custom endpoint roles."""

    iri: str
    subject_role: TermRole | None = field(default=None, kw_only=True)
    object_role: TermRole | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Reject malformed IRIs and roles before accepting the declaration."""
        if not isinstance(self.iri, str):
            raise InputError("A fact predicate requires an IRI string")
        iri = str(self.iri)
        absolute_iri(iri)
        object.__setattr__(self, "iri", iri)
        for role in (cast(object, self.subject_role), cast(object, self.object_role)):
            if role is not None and (type(role) is not str or role not in _ROLES):
                raise InputError("Select an explicit supported endpoint role")


@dataclass(frozen=True)
class FactGraph:
    """Distinguish a standalone graph, dataset default and actual named graph.

    A blank graph selector belongs to its owned snapshot and matches by object
    identity only. Its label cannot identify a graph in an unrelated snapshot.
    """

    kind: Choice["graph", "default", "named"]
    name: URIRef | BNode | None = None

    def __post_init__(self) -> None:
        """Require graph names only for explicit named-graph selections."""
        if self.kind not in ("graph", "default", "named"):
            raise InputError("Select standalone, default or named graph scope")
        if self.kind == "named":
            if not isinstance(self.name, URIRef | BNode):
                raise InputError("Named graph scope requires an actual RDF name")
            if isinstance(self.name, URIRef):
                absolute_iri(str(self.name))
        elif self.name is not None:
            raise InputError("Only a named graph scope has a graph name")


@dataclass(frozen=True)
class FactSelection:
    """Own exact predicates, endpoint IRIs, source roles and graph selections.

    Empty entities selects all endpoints. Omitted graphs retains the supplied
    selection; an empty graph tuple explicitly selects no graph. Source roles
    are local/imports/lookup, never guessed document identity or array position.
    """

    predicates: tuple[FactPredicate, ...]
    entities: tuple[str, ...] = field(default=(), kw_only=True)
    positions: tuple[TermPosition, ...] = field(
        default=("subject", "object"), kw_only=True
    )
    scopes: tuple[str, ...] = field(default=("local",), kw_only=True)
    graphs: tuple[FactGraph, ...] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Capture bounded declaration tuples and reject ambiguous duplicates."""
        for name, expected in (
            ("predicates", FactPredicate),
            ("entities", str),
            ("positions", str),
            ("scopes", str),
        ):
            object.__setattr__(self, name, _own(getattr(self, name), expected, name))
        if not self.predicates or len({p.iri for p in self.predicates}) != len(
            self.predicates
        ):
            raise InputError("Select nonempty unique fact predicates")
        if not self.positions or set(self.positions) - {
            "subject",
            "predicate",
            "object",
            "datatype",
            "graph",
        }:
            raise InputError("Select supported fact positions")
        if not self.scopes or set(self.scopes) - {"local", "imports", "lookup"}:
            raise InputError("Select explicit source roles")
        for iri in self.entities:
            absolute_iri(iri)
        if self.graphs is not None:
            object.__setattr__(self, "graphs", _own(self.graphs, FactGraph, "graphs"))


@dataclass(frozen=True)
class SelectedFact:
    """One selected original assertion and distinct per-position role evidence."""

    statement: Statement
    source_scope: str
    graph: FactGraph
    occurrences: tuple[TermRoleOccurrence, ...]


@dataclass(frozen=True)
class SelectedFactDifference:
    """An exact asserted addition/removal, without business or OWL meaning."""

    status: Choice["added", "removed"]
    fact: SelectedFact


@dataclass(frozen=True)
class AnonymousFactComparison:
    """Joint maintained correspondence without fabricated owner attribution."""

    source_scopes: tuple[str, ...]
    before: tuple[SelectedFact, ...]
    after: tuple[SelectedFact, ...]
    report: Report


@dataclass(frozen=True)
class SelectedFactDifferences:
    """Exact named differences, raw anonymous comparisons and completion truth."""

    differences: tuple[SelectedFactDifference, ...]
    anonymous: tuple[AnonymousFactComparison, ...]
    report: Report
