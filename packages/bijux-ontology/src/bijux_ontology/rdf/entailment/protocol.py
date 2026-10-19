"""Bounded exact term inventories shared by entailment host and owned worker."""

from rdflib import BNode, Graph
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.queries.protocol import QueryTerm, read_term, term_data
from bijux_ontology.rdf.terms import bound_node, validate_triple

from .types import GeneralizedTriple


def encode_term(node: Node | None, budget: Budget) -> dict[str, object] | None:
    """Reserve exact scalar and record ownership before the existing encoder."""
    budget.consume("work")
    budget.consume("states", 5)
    budget.consume("bytes", 128)
    if node is not None:
        bound_node(node, budget)
    return term_data(node)


def decode_term(
    value: object, blanks: dict[str, BNode], budget: Budget
) -> QueryTerm | None:
    """Bind one bounded term within this joint protocol document only."""
    budget.consume("bytes", json_bound(value, budget))
    budget.consume("states", 5)
    budget.consume("bytes", 128)
    term = read_term(value, blanks)
    if term is not None:
        bound_node(term, budget)
    return term


def encode_rows(rows: object, budget: Budget) -> list[list[object]]:
    """Export graph or already captured triples with exact lexical terms."""
    if not isinstance(rows, (Graph, tuple)):
        raise InputError("Entailment output requires an owned triple inventory")
    result: list[list[object]] = []
    for triple in rows:
        budget.consume("work")
        budget.consume("triples")
        budget.consume("states", 4)
        result.append([encode_term(node, budget) for node in triple])
    return result


def decode_rows(
    value: object, blanks: dict[str, BNode], budget: Budget, *, ordinary: bool
) -> tuple[GeneralizedTriple, ...]:
    """Capture every retained triple without interpreting lexical RDF values."""
    if not isinstance(value, list):
        raise InputError("Entailment triples require arrays")
    budget.consume("states", len(value))
    budget.consume("bytes", 64 + 16 * len(value))
    result = []
    for row in value:
        budget.consume("work")
        budget.consume("triples")
        if not isinstance(row, list) or len(row) != 3:
            raise InputError("Entailment triple must have three terms")
        terms = tuple(decode_term(term, blanks, budget) for term in row)
        if any(term is None for term in terms):
            raise InputError("Entailment triples cannot contain unbound values")
        subject, predicate, target = terms
        assert subject is not None and predicate is not None and target is not None
        if ordinary:
            validate_triple(subject, predicate, target)
        result.append((subject, predicate, target))
    return tuple(result)
