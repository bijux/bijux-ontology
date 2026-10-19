"""Exact tagged fact projection with one owned anonymous identity space."""

import json

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_text, term_key
from bijux_ontology.vocabulary.sources.types import source_role

from .types import SelectedFact

_BASE = "urn:bijux:ontology:selected-facts:"


class FactProjection:
    """Encode selected record positions for the maintained RDF comparator.

    Original blank terms share one side-local mapping. Tagged exact nonblank
    terms cannot collide with structural fields or generated record nodes.
    There is no correspondence inferred from labels in the opposite snapshot.
    """

    def __init__(self, graph: Graph, budget: Budget) -> None:
        """Own one operation-local projection and side-local blank mapping."""
        self.graph = graph
        self.budget = budget
        self.blanks: dict[BNode, BNode] = {}
        self.count = 0

    def term(self, value: Node | None) -> Node:
        """Reuse a side-local blank identity or retain a tagged exact RDF term."""
        self.budget.consume("work")
        if isinstance(value, BNode):
            if value not in self.blanks:
                self.budget.consume("states")
                self.budget.consume("bytes", 128)
                self.blanks[value] = BNode("term:" + str(len(self.blanks)))
            return self.blanks[value]
        key = term_key(value)
        allowance = 64 + 6 * sum(len(part or "") for part in key)
        self.budget.consume("work", allowance)
        self.budget.consume("bytes", allowance)
        text = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
        bound_text(text, self.budget)
        return Literal(text, normalize=False)

    def add(self, fact: SelectedFact) -> None:
        """Reserve all record edges before adding the qualified projection."""
        self.budget.consume("work", 6)
        self.budget.consume("triples", 6)
        self.budget.consume("states", 7)
        self.budget.consume("bytes", 768)
        node = BNode("record:" + str(self.count))
        self.count += 1
        values = (
            Literal(source_role(fact.source_scope)),
            Literal(fact.graph.kind),
            self.term(fact.graph.name),
            *(self.term(value) for value in fact.statement),
        )
        for index, value in enumerate(values):
            self.graph.add((node, URIRef(_BASE + str(index)), value))
