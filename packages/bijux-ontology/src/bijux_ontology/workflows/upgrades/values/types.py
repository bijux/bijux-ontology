"""Exact stored occurrences and explicit before/after controlled field policies."""

from dataclasses import KW_ONLY, dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.term import Node

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Report

ValueTransition = Literal[
    "introduced", "persistent", "resolved", "unchanged", "uncertain"
]


@dataclass(frozen=True)
class ControlledField:
    """One explicitly matched predicate policy over unchanged stored RDF values.

    Root, scheme, collection and lifecycle decisions belong to the supplied
    enumeration evidence. Reverse selects subjects of assertions whose object
    is the record; it does not reverse or reinterpret the stored statement.
    """

    identifier: str
    predicate: str
    before: ValueEnumeration
    after: ValueEnumeration
    _: KW_ONLY
    reverse: bool = False

    def __post_init__(self) -> None:
        """Own accepted textual metadata without coercing arbitrary input objects."""
        budget = Budget()
        for text in (self.identifier, self.predicate):
            if not isinstance(text, str) or isinstance(text, BNode | RdfLiteral):
                raise InputError("Controlled fields require explicit text metadata")
            bound_text(text, budget)
        if not self.identifier or type(self.reverse) is not bool:
            raise InputError("Controlled fields require a name and boolean direction")
        absolute_iri(self.predicate)
        if not all(isinstance(e, ValueEnumeration) for e in (self.before, self.after)):
            raise InputError("Controlled fields require before/after ValueEnumeration")
        object.__setattr__(self, "identifier", str(self.identifier))
        object.__setattr__(self, "predicate", str(self.predicate))


@dataclass(frozen=True)
class ValueReplacement:
    """An explicit advisory substitution, never equivalence or migration acceptance."""

    before: URIRef | RdfLiteral
    after: URIRef | RdfLiteral

    def __post_init__(self) -> None:
        """Require exact named or literal endpoints without lexical guessing."""
        budget = Budget()
        for term in (self.before, self.after):
            if not isinstance(term, URIRef | RdfLiteral):
                raise InputError("Value replacements require named IRIs or literals")
            bound_node(term, budget)


@dataclass(frozen=True)
class StoredValueImpact:
    """One actual selected statement with independently checked policy membership."""

    record: URIRef
    field: str
    statement: tuple[Node, Node, Node]
    value: Node
    graph: URIRef | BNode | None
    graph_kind: Literal["graph", "default", "named"]
    reverse: bool
    before: bool | None
    after: bool | None
    status: ValueTransition


@dataclass(frozen=True)
class ReplacementEvaluation:
    """A proposed value's after-policy membership for an actual stored occurrence."""

    use: StoredValueImpact
    proposal: ValueReplacement
    admissible: bool | None


@dataclass(frozen=True)
class ControlledValueUpgrade:
    """Finite raw release checks, record evidence and separate advisory proposals."""

    before: Report
    after: Report
    uses: tuple[StoredValueImpact, ...]
    replacements: tuple[ReplacementEvaluation, ...]
    report: Report

    @property
    def demonstrated_break(self) -> bool:
        """Require established old permission and new exclusion of a stored value."""
        return any(use.status == "introduced" for use in self.uses)
