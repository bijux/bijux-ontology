"""Caller-authored relation roles, physical occurrences and identity allocations."""

from dataclasses import dataclass, field
from typing import Literal as LiteralType

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_node

Resource = URIRef | BNode
Value = Resource | Literal


def _term(value: object, expected: type[object], budget: Budget) -> None:
    """Bound ordinary immutable RDF terms before inspecting contract metadata."""
    if not isinstance(value, expected):
        raise InputError("Relation contracts require typed RDF terms")
    assert isinstance(value, URIRef | BNode | Literal)
    bound_node(value, budget)


@dataclass(frozen=True)
class RelationRole:
    """A directed participant edge relative to the relation event.

    Outgoing means event→participant; incoming means participant→event.
    The direction and predicate carry caller meaning, without name inference.
    """

    predicate: URIRef
    direction: LiteralType["outgoing", "incoming"] = "outgoing"

    def __post_init__(self) -> None:
        """Reserve named participant predicates and finite orientation."""
        _term(self.predicate, URIRef, Budget())
        if self.predicate == RDF.type or self.direction not in ("outgoing", "incoming"):
            raise InputError(
                "Use a non-type participant predicate and explicit direction"
            )


@dataclass(frozen=True)
class RelationPattern:
    """Declare a binary predicate's application relation class and two roles.

    This is a supplied semantic contract, not an inferred OWL equivalence.
    Negative patterns cannot be converted to ordinary positive RDF edges.
    """

    predicate: URIRef
    relation_class: URIRef
    subject_role: RelationRole
    object_role: RelationRole
    assertion: LiteralType["positive", "negative"]

    def __post_init__(self) -> None:
        """Require distinct typed roles and an explicit assertion interpretation."""
        budget = Budget()
        for value in (self.predicate, self.relation_class):
            _term(value, URIRef, budget)
        if (
            type(self.subject_role) is not RelationRole
            or type(self.object_role) is not RelationRole
            or self.subject_role == self.object_role
            or self.assertion not in ("positive", "negative")
        ):
            raise InputError(
                "Declare distinct roles and an explicit assertion polarity"
            )


@dataclass(frozen=True)
class RelationQualifier:
    """One exact caller-supplied outgoing event attribute, never a guessed value."""

    predicate: URIRef
    value: Value

    def __post_init__(self) -> None:
        """Retain the original literal spelling or actual owned resource witness."""
        budget = Budget()
        _term(self.predicate, URIRef, budget)
        if not isinstance(self.value, URIRef | BNode | Literal):
            raise InputError("Relation qualifiers require actual RDF values")
        bound_node(self.value, budget)


@dataclass(frozen=True)
class BinaryOccurrence:
    """Replace one exact binary quad using a caller-allocated fresh event IRI.

    None selects only the default graph, never every graph. The event IRI must
    be unused in the authorized source scope. Blanks in participants, qualifiers
    and graph selectors must be exact terms exported by that source owner.
    """

    subject: Resource
    value: Value
    event: URIRef
    qualifiers: tuple[RelationQualifier, ...] = ()
    graph: Resource | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Require finite immutable qualifiers and resource positions."""
        budget = Budget()
        if not isinstance(self.subject, URIRef | BNode):
            raise InputError("Binary relation subjects require resources")
        if not isinstance(self.value, URIRef | BNode | Literal):
            raise InputError("Binary relation objects require RDF values")
        for value in (self.subject, self.value, self.event, self.graph):
            if value is not None:
                bound_node(value, budget)
        _term(self.event, URIRef, budget)
        if self.graph is not None and not isinstance(self.graph, URIRef | BNode):
            raise InputError("Relation graph selectors require resources")
        if type(self.qualifiers) is not tuple:
            raise InputError("Relation qualifiers require immutable tuples")
        budget.consume("states", len(self.qualifiers))
        if any(type(item) is not RelationQualifier for item in self.qualifiers):
            raise InputError("Use typed relation qualifier records")


@dataclass(frozen=True)
class RelationOccurrence:
    """Select one original relation event in exactly one physical graph."""

    event: Resource
    graph: Resource | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Keep actual blank identity rather than accepting parser-label strings."""
        budget = Budget()
        for value in (self.event, self.graph):
            if value is not None:
                if not isinstance(value, URIRef | BNode):
                    raise InputError("Relation occurrences require resource selectors")
                bound_node(value, budget)
