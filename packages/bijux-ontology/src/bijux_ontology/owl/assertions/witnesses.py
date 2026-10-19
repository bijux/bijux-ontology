"""Contradiction matching with original assertion and identity premises."""

from __future__ import annotations

from itertools import combinations
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.owl.obligations import OntologyObligation

from .context import AssertionContext, _Row
from .patterns import Resource
from .types import Statement, TermKey


def edge_witnesses(
    state: AssertionContext,
    irreflexive: tuple[URIRef, ...],
    asymmetric: tuple[URIRef, ...],
) -> None:
    """Match irreflexive/asymmetric assertions modulo proved identity."""
    for predicate in irreflexive:
        kind, declaration = state.kind(predicate)
        if kind != "object":
            state.unknown("Irreflexivity requires a declared object property")
            continue
        characteristic = state.patterns.statement(
            predicate, RDF.type, OWL.IrreflexiveProperty
        )
        for row in state.rows(predicate):
            state.operation.consume("work")
            assert isinstance(row.value, URIRef | BNode)
            proof = state.equal(row.subject, row.value)
            if proof is not None:
                state.witness(
                    "irreflexive",
                    (characteristic, *declaration, row.premise, *proof),
                )
    for predicate in asymmetric:
        kind, declaration = state.kind(predicate)
        if kind != "object":
            state.unknown("Asymmetry requires a declared object property")
            continue
        marker = state.patterns.statement(predicate, RDF.type, OWL.AsymmetricProperty)
        rows = state.rows(predicate)
        for index, left in enumerate(rows):
            for right_index in range(index, len(rows)):
                state.operation.consume("work")
                right = rows[right_index]
                assert isinstance(left.value, URIRef | BNode) and isinstance(
                    right.value, URIRef | BNode
                )
                first, second = (
                    state.equal(left.subject, right.value),
                    state.equal(left.value, right.subject),
                )
                if first is not None and second is not None:
                    state.witness(
                        "asymmetric",
                        (
                            marker,
                            *declaration,
                            left.premise,
                            right.premise,
                            *first,
                            *second,
                        ),
                    )


def same_edge(
    state: AssertionContext, left: _Row, right: _Row, kind: str
) -> tuple[Statement, ...] | None:
    """Prove the same subject/value tuple using resource or actual datatype identity."""
    first = state.equal(left.subject, right.subject)
    if first is None:
        return None
    if kind == "object":
        assert isinstance(left.value, URIRef | BNode) and isinstance(
            right.value, URIRef | BNode
        )
        second = state.equal(left.value, right.value)
        return (*first, *second) if second is not None else None
    assert isinstance(left.value, Literal) and isinstance(right.value, Literal)
    return first if state.literal_equal(left.value, right.value) == "equal" else None


def disjoint(state: AssertionContext) -> None:
    """Check explicit property disjointness and standard finite property groups."""
    pairs: list[tuple[URIRef, URIRef, tuple[Statement, ...]]] = []
    for left, _, right in state.graph.triples((None, OWL.propertyDisjointWith, None)):
        state.operation.consume("work")
        premise = state.patterns.statement(left, OWL.propertyDisjointWith, right)
        if not isinstance(left, URIRef) or not isinstance(right, URIRef):
            state.unknown(
                "Disjoint property expressions require supported named properties"
            )
            continue
        state.operation.consume("states")
        pairs.append((left, right, (premise,)))
    for group in state.graph.subjects(RDF.type, OWL.AllDisjointProperties):
        state.operation.consume("work")
        heads = state.patterns.objects(group, OWL.members, maximum=2)
        if len(heads) != 1:
            state.unknown("A disjoint property group requires one member list")
            continue
        try:
            values, proof = state.patterns.listed(heads[0])
        except LimitExceeded:
            raise
        except InputError:
            state.unknown("A disjoint property group has a malformed member list")
            continue
        if len(values) < 2 or any(not isinstance(value, URIRef) for value in values):
            state.unknown("A disjoint property group requires named members")
            continue
        base = (
            state.patterns.statement(group, RDF.type, OWL.AllDisjointProperties),
            state.patterns.statement(group, OWL.members, heads[0]),
            *proof,
        )
        for left, right in combinations(values, 2):
            state.operation.consume("states")
            pairs.append((cast(URIRef, left), cast(URIRef, right), base))
    for left, right, premises in pairs:
        first, a = state.kind(left)
        second, b = state.kind(right)
        if first is None or first != second:
            state.unknown("Disjoint properties lack one compatible logical category")
            continue
        for x in state.rows(left):
            for y in state.rows(right):
                state.operation.consume("work")
                edge_proof = same_edge(state, x, y, first)
                if edge_proof is not None:
                    state.witness(
                        "disjoint_properties",
                        (*premises, *a, *b, x.premise, y.premise, *edge_proof),
                    )


def negatives(state: AssertionContext) -> None:
    """Match explicit negatives to actual positive assertions."""
    for negative in state.patterns.negatives():
        state.mark_named(negative.subject)
        if isinstance(negative.value, URIRef | BNode):
            state.mark_named(negative.value)
        kind, declaration = state.kind(negative.predicate)
        if kind is None or (kind == "data") != negative.data:
            state.unknown(
                "A negative assertion lacks its supported logical property category"
            )
            continue
        if isinstance(negative.value, Literal):
            state.validate(negative.value, (*negative.premises, *declaration))
        row = _Row(negative.subject, negative.value, negative.premises[0])
        for positive in state.rows(negative.predicate):
            state.operation.consume("work")
            proof = same_edge(state, row, positive, kind)
            if proof is not None:
                state.witness(
                    "negative_assertion",
                    (*negative.premises, *declaration, positive.premise, *proof),
                )


def differences(state: AssertionContext) -> None:
    """Only actual difference axioms can contradict established individual identity."""
    for left, _, right in state.graph.triples((None, OWL.differentFrom, None)):
        state.operation.consume("work")
        premise = state.patterns.statement(left, OWL.differentFrom, right)
        if not isinstance(left, URIRef | BNode) or not isinstance(
            right, URIRef | BNode
        ):
            state.unknown("Different-individual assertions require resource endpoints")
            continue
        proof = state.equal(left, right)
        if proof is not None:
            state.witness("different_individuals", (premise, *proof))
    for group in state.graph.subjects(RDF.type, OWL.AllDifferent):
        state.operation.consume("work")
        heads = [
            (predicate, head)
            for predicate in (OWL.distinctMembers, OWL.members)
            for head in state.patterns.objects(group, predicate, maximum=2)
        ]
        if len(heads) != 1:
            state.unknown("An all-different group requires one explicit member list")
            continue
        predicate, head = heads[0]
        try:
            values, proof = state.patterns.listed(head)
        except LimitExceeded:
            raise
        except InputError:
            state.unknown("An all-different group has a malformed member list")
            continue
        if len(values) < 2 or any(
            not isinstance(value, URIRef | BNode) for value in values
        ):
            state.unknown("All-different members require at least two resources")
            continue
        base = (
            state.patterns.statement(group, RDF.type, OWL.AllDifferent),
            state.patterns.statement(group, predicate, head),
            *proof,
        )
        for left, right in combinations(values, 2):
            state.operation.consume("work")
            identity = state.equal(cast(Resource, left), cast(Resource, right))
            if identity is not None:
                state.witness("different_individuals", (*base, *identity))


def type_witnesses(state: AssertionContext) -> None:
    """Reuse finite named coinherence proofs for established same individuals."""
    groups: dict[TermKey, list[tuple[Resource, URIRef, Statement]]] = {}
    for subject, kind, premise in state.types:
        state.operation.consume("states")
        groups.setdefault(state.identity.find(state.key(subject)), []).append(
            (subject, kind, premise)
        )
    for rows in groups.values():
        state.operation.consume("work")
        state.operation.consume("states", len(rows))
        actual = tuple(str(row[1]) for row in rows)
        answer = state.proofs.answer(
            OntologyObligation("required_coinstance", actual, regime="owl-dl")
        )
        if answer.status == "contradicted":
            proof: list[Statement] = []
            representative = rows[0][0]
            for subject, _, premise in rows:
                identity = state.equal(subject, representative)
                assert identity is not None
                state.operation.consume("states", len(identity) + 1)
                proof.extend((premise, *identity))
            proof.extend(state.patterns.statement(*edge) for edge in answer.witness)
            state.witness("disjoint_types", tuple(proof))
