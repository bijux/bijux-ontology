"""Finite equality implications without inferred RDF saturation."""

from __future__ import annotations

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF

from .context import AssertionContext, _Row
from .key_join import KeyJoin
from .patterns import KeyPattern


def functional_pair(
    state: AssertionContext,
    predicate: URIRef,
    inverse: bool,
    left: _Row,
    right: _Row,
) -> None:
    """Apply one affected pair, retaining only complete original-premise evidence."""
    state.operation.consume("work")
    kind, declaration = state.kind(predicate)
    marker = OWL.InverseFunctionalProperty if inverse else OWL.FunctionalProperty
    characteristic = state.patterns.statement(predicate, RDF.type, marker)
    if inverse:
        assert isinstance(left.value, URIRef | BNode) and isinstance(
            right.value, URIRef | BNode
        )
        proof = state.equal(left.value, right.value)
    else:
        proof = state.equal(left.subject, right.subject)
    assert proof is not None
    premises = (characteristic, *declaration, left.premise, right.premise, *proof)
    if inverse:
        state.identity.merge(
            state.key(left.subject),
            state.key(right.subject),
            "inverse_functional",
            premises,
        )
    elif kind == "object":
        state.identity.merge(
            state.key(left.value), state.key(right.value), "functional", premises
        )
    else:
        assert isinstance(left.value, Literal) and isinstance(right.value, Literal)
        if state.literal_equal(left.value, right.value) == "different":
            state.witness("functional_data", premises)


def key_identities(state: AssertionContext, keys: tuple[KeyPattern, ...]) -> bool:
    """Join actual shared witnesses; properties never become functional."""
    for key in keys:
        join = KeyJoin(state, key)
        for position, (left, left_proof) in enumerate(join.members):
            for right_position in join.candidates(position):
                right, right_proof = join.members[right_position]
                state.operation.consume("work")
                if state.equal(left, right) is not None:
                    continue
                proof = [*key.premises, *left_proof, *right_proof]
                complete = True
                for predicate in key.properties:
                    state.operation.consume("work")
                    kind, declaration = state.kind(predicate)
                    if kind is None:
                        complete = False
                        break
                    common = None
                    unnamed_common = False
                    for a in join.subject_rows(predicate, left):
                        state.operation.consume("work")
                        first = state.equal(left, a.subject)
                        assert first is not None
                        targets = (
                            join.value_rows(predicate, right, a.value)
                            if isinstance(a.value, URIRef | BNode)
                            else join.subject_rows(predicate, right)
                        )
                        for b in targets:
                            state.operation.consume("work")
                            second = state.equal(right, b.subject)
                            assert second is not None
                            if kind == "object":
                                assert isinstance(
                                    a.value, URIRef | BNode
                                ) and isinstance(b.value, URIRef | BNode)
                                identity = state.equal(a.value, b.value)
                                assert identity is not None
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
                if complete and state.identity.merge(
                    state.key(left), state.key(right), "key", proof
                ):
                    # The affected-key queue will rebuild value indexes against
                    # the new classes before considering another inference.
                    return True
    return False
