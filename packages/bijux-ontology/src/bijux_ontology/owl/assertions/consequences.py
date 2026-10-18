"""Finite equality implications without inferred RDF saturation."""

from __future__ import annotations

from itertools import chain, combinations

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF

from .context import AssertionContext
from .patterns import KeyPattern, Resource
from .types import Statement


def functionality(
    state: AssertionContext, functional: tuple[URIRef, ...], inverse: tuple[URIRef, ...]
) -> bool:
    """Establish identity only; two resource IRIs alone never supply inequality."""
    changed = False
    for predicate, inverse_ in chain(
        ((p, False) for p in functional), ((p, True) for p in inverse)
    ):
        state.operation.consume("work")
        kind, declaration = state.kind(predicate)
        if kind is None or inverse_ and kind != "object":
            state.unknown("Property functionality lacks a supported logical category")
            continue
        marker = OWL.InverseFunctionalProperty if inverse_ else OWL.FunctionalProperty
        characteristic = state.patterns.statement(predicate, RDF.type, marker)
        for left, right in combinations(state.rows(predicate), 2):
            state.operation.consume("work")
            if inverse_:
                assert isinstance(left.value, URIRef | BNode) and isinstance(
                    right.value, URIRef | BNode
                )
                proof = state.equal(left.value, right.value)
            else:
                proof = state.equal(left.subject, right.subject)
            if proof is None:
                continue
            premises = (
                characteristic,
                *declaration,
                left.premise,
                right.premise,
                *proof,
            )
            if inverse_:
                changed |= state.identity.merge(
                    state.key(left.subject),
                    state.key(right.subject),
                    "inverse_functional",
                    premises,
                )
            elif kind == "object":
                changed |= state.identity.merge(
                    state.key(left.value),
                    state.key(right.value),
                    "functional",
                    premises,
                )
            else:
                assert isinstance(left.value, Literal) and isinstance(
                    right.value, Literal
                )
                relation = state.literal_equal(left.value, right.value)
                if relation == "different":
                    state.witness("functional_data", premises)
    return changed


def key_identities(state: AssertionContext, keys: tuple[KeyPattern, ...]) -> bool:
    """Use shared key witnesses without making properties functional."""
    changed = False
    for key in keys:
        members: list[tuple[Resource, tuple[Statement, ...]]] = []
        for subject, actual, premise in state.types:
            state.operation.consume("work")
            ancestry = state.member(actual, key.klass)
            named = state.named_proof(subject)
            if ancestry is None or named is None:
                continue
            state.operation.consume("states")
            members.append((subject, (premise, *ancestry, *named)))
        if key.klass == OWL.Thing:
            for predicate in key.properties:
                for row in state.rows(predicate):
                    state.operation.consume("work")
                    named = state.named_proof(row.subject)
                    if named is not None:
                        state.operation.consume("states")
                        members.append((row.subject, (row.premise, *named)))
        state.operation.consume("states", len(members))
        state.operation.consume("bytes", 8 * len(members))
        for (left, left_proof), (right, right_proof) in combinations(members, 2):
            state.operation.consume("work")
            if state.equal(left, right) is not None:
                continue
            proof = [*key.premises, *left_proof, *right_proof]
            complete = True
            for predicate in key.properties:
                state.operation.consume("work")
                kind, declaration = state.kind(predicate)
                if kind is None:
                    state.unknown(
                        "A key property lacks an unambiguous object/data declaration"
                    )
                    complete = False
                    break
                common = None
                unnamed_common = False
                for a in state.rows(predicate):
                    state.operation.consume("work")
                    first = state.equal(left, a.subject)
                    if first is None:
                        continue
                    for b in state.rows(predicate):
                        state.operation.consume("work")
                        second = state.equal(right, b.subject)
                        if second is None:
                            continue
                        if kind == "object":
                            assert isinstance(a.value, URIRef | BNode) and isinstance(
                                b.value, URIRef | BNode
                            )
                            identity = state.equal(a.value, b.value)
                            if identity is None:
                                continue
                            named = state.named_proof(a.value)
                            if named is None:
                                unnamed_common = True
                                continue
                            common = (
                                *first,
                                *second,
                                *identity,
                                *named,
                                a.premise,
                                b.premise,
                            )
                        else:
                            assert isinstance(a.value, Literal) and isinstance(
                                b.value, Literal
                            )
                            if state.literal_equal(a.value, b.value) != "equal":
                                continue
                            common = (*first, *second, a.premise, b.premise)
                        break
                    if common is not None:
                        break
                if common is None:
                    if unnamed_common:
                        state.unknown(
                            "A common anonymous key filler is not established named"
                        )
                    complete = False
                    break
                state.operation.consume("states", len(common) + len(declaration))
                proof.extend((*declaration, *common))
            if complete:
                changed |= state.identity.merge(
                    state.key(left), state.key(right), "key", proof
                )
    return changed
