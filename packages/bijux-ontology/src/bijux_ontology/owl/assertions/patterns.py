"""Bounded original RDF patterns for finite assertion characteristics and keys."""

from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import Statement

Resource = URIRef | BNode
Value = Resource | Literal


@dataclass(frozen=True)
class KeyPattern:
    """One named class key and its original complete RDF list premises."""

    klass: URIRef
    properties: tuple[URIRef, ...]
    premises: tuple[Statement, ...]


@dataclass(frozen=True)
class NegativePattern:
    """One supported negative assertion, without treating absence as negation."""

    subject: Resource
    predicate: URIRef
    value: Value
    data: bool
    premises: tuple[Statement, ...]


class Patterns:
    """Use maintained graph/list readers; retain no derived RDF assertions."""

    def __init__(self, graph: Graph, budget: Budget, issues: IssueCollector) -> None:
        """Share counters and original graph identity with the witness operation."""
        self.graph, self.budget, self.issues = graph, budget, issues

    def unknown(self, message: str) -> None:
        """Keep unsupported scope explicit without formatting arbitrary terms."""
        self.issues.append(
            Issue("owl.assertion_unverified", message, kind="unverified")
        )

    def statement(self, subject: Node, predicate: Node, value: Node) -> Statement:
        """Qualify exact term metadata before proof keys are copied or hashed."""
        for term in (subject, predicate, value):
            bound_node(term, self.budget)
        self.budget.consume("states")
        self.budget.consume("bytes", 96)
        return term_key(subject), term_key(predicate), term_key(value)

    def objects(
        self, subject: Node, predicate: URIRef, *, maximum: int | None = None
    ) -> tuple[Node, ...]:
        """Capture actual values before any tuple allocation or role decision."""
        result = []
        for value in self.graph.objects(subject, predicate):
            self.budget.consume("work")
            bound_node(value, self.budget)
            self.budget.consume("states")
            self.budget.consume("bytes", 16)
            result.append(value)
            if maximum is not None and len(result) >= maximum:
                break
        return tuple(result)

    def listed(self, head: Node) -> tuple[tuple[Node, ...], tuple[Statement, ...]]:
        """Decode one bounded list with original first/rest edges as proof premises."""
        bound_node(head, self.budget)
        values = []
        premises = []
        cell = head
        for value in iter_rdf_list(
            self.graph,
            head,
            budget=self.budget,
            max_items=self.budget.limits.max_states,
        ):
            bound_node(value, self.budget)
            tail = self.objects(cell, RDF.rest, maximum=2)
            self.budget.consume("states", 3)
            self.budget.consume("bytes", 48)
            values.append(value)
            premises.append(self.statement(cell, RDF.first, value))
            premises.append(self.statement(cell, RDF.rest, tail[0]))
            cell = tail[0]
        return tuple(values), tuple(premises)

    def keys(self) -> tuple[KeyPattern, ...]:
        """Only complete named-class/named-property keys enter the finite rules."""
        result = []
        for klass, _, head in self.graph.triples((None, OWL.hasKey, None)):
            self.budget.consume("work")
            premise = self.statement(klass, OWL.hasKey, head)
            if not isinstance(klass, URIRef):
                self.unknown(
                    "Anonymous key class expressions are outside the finite scope"
                )
                continue
            try:
                values, proof = self.listed(head)
            except LimitExceeded:
                raise
            except InputError:
                self.unknown("A key property list is malformed or cyclic")
                continue
            if not values or any(not isinstance(value, URIRef) for value in values):
                self.unknown("A supported key needs at least one named property")
                continue
            self.budget.consume("states")
            self.budget.consume("bytes", 96)
            result.append(
                KeyPattern(klass, cast(tuple[URIRef, ...], values), (premise, *proof))
            )
        return tuple(result)

    def negatives(self) -> tuple[NegativePattern, ...]:
        """Qualify actual OWL negative assertion anchors and exact target roles."""
        result = []
        for node in self.graph.subjects(RDF.type, OWL.NegativePropertyAssertion):
            self.budget.consume("work")
            bound_node(node, self.budget)
            source = self.objects(node, OWL.sourceIndividual, maximum=2)
            property_ = self.objects(node, OWL.assertionProperty, maximum=2)
            target = self.objects(node, OWL.targetIndividual, maximum=2)
            literal = self.objects(node, OWL.targetValue, maximum=2)
            if (
                len(source) != 1
                or not isinstance(source[0], URIRef | BNode)
                or len(property_) != 1
                or not isinstance(property_[0], URIRef)
                or len(target) + len(literal) != 1
                or target
                and not isinstance(target[0], URIRef | BNode)
                or literal
                and not isinstance(literal[0], Literal)
            ):
                self.unknown(
                    "Negative assertion requires one source, property and typed target"
                )
                continue
            value = (literal or target)[0]
            target_predicate = OWL.targetValue if literal else OWL.targetIndividual
            proof = tuple(
                self.statement(node, predicate, item)
                for predicate, item in (
                    (RDF.type, OWL.NegativePropertyAssertion),
                    (OWL.sourceIndividual, source[0]),
                    (OWL.assertionProperty, property_[0]),
                    (target_predicate, value),
                )
            )
            self.budget.consume("states")
            self.budget.consume("bytes", 96)
            result.append(
                NegativePattern(
                    source[0], property_[0], cast(Value, value), bool(literal), proof
                )
            )
        return tuple(result)
