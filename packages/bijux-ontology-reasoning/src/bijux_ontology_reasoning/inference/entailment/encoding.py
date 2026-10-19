"""A bounded finite encoding of existing typed OWL requests, not a new AST."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from ...axioms import types as t


class UnsupportedQuery(ResourceError):
    """The supplied typed obligation is outside the selected finite profile."""


def _iri(raw: str, budget: Budget) -> str:
    if not isinstance(raw, str):
        raise InputError("OWL query IRIs require text")
    bound_text(raw, budget)
    return str(absolute_iri(raw))


def _entity(raw: t.NamedEntity, kind: str, budget: Budget) -> dict[str, object]:
    if type(raw) is not t.NamedEntity or raw.kind != kind:
        raise UnsupportedQuery("Query entity does not have the required OWL role")
    return {"kind": kind, "iri": _iri(raw.iri, budget)}


def _expression(
    raw: t.ClassExpression, budget: Budget, depth: int
) -> dict[str, object]:
    budget.consume("work")
    budget.consume("states")
    budget.consume("bytes", 128)
    if depth > budget.limits.max_depth // 4:
        raise LimitExceeded("Query class expression exceeds max_depth")
    if depth > 64:
        raise UnsupportedQuery("Query class expression exceeds the supported depth")
    if type(raw) is t.NamedEntity:
        return _entity(raw, "class", budget)
    if type(raw) is t.ClassComplement:
        return {
            "kind": "complement",
            "operand": _expression(raw.operand, budget, depth + 1),
        }
    if type(raw) is t.ClassJunction:
        if (
            raw.operator not in ("intersection", "union")
            or type(raw.operands) is not tuple
            or len(raw.operands) < 2
        ):
            raise UnsupportedQuery("Query junction requires its exact finite operands")
        budget.consume("states", len(raw.operands))
        budget.consume("bytes", 32 * len(raw.operands))
        return {
            "kind": raw.operator,
            "operands": [_expression(x, budget, depth + 1) for x in raw.operands],
        }
    if type(raw) is t.ObjectQuantifier:
        if (
            raw.quantifier not in ("some", "all")
            or type(raw.property) is not t.ObjectProperty
            or type(raw.property.inverse) is not bool
        ):
            raise UnsupportedQuery("Query restriction requires a typed object property")
        return {
            "kind": raw.quantifier,
            "property": _iri(raw.property.iri, budget),
            "inverse": raw.property.inverse,
            "filler": _expression(raw.filler, budget, depth + 1),
        }
    raise UnsupportedQuery("This typed class expression is outside the query profile")


def encode_axiom(raw: t.ProjectedAxiom, budget: Budget) -> dict[str, object]:
    """Qualify supported typed class restrictions before native entry."""
    budget.consume("work")
    budget.consume("states", 2)
    budget.consume("bytes", 256)
    if type(raw) not in (t.SubClassOf, t.ClassAssertion):
        raise UnsupportedQuery(
            "Only subclass and class-assertion queries are qualified"
        )
    if type(raw.annotations) is not tuple or raw.annotations:
        raise UnsupportedQuery("Annotated query axioms are outside the query profile")
    if isinstance(raw, t.SubClassOf):
        return {
            "kind": "subclass",
            "sub": _expression(raw.sub_class, budget, 1),
            "super": _expression(raw.super_class, budget, 1),
        }
    assert isinstance(raw, t.ClassAssertion)
    return {
        "kind": "class_assertion",
        "individual": _entity(raw.individual, "named_individual", budget),
        "expression": _expression(raw.expression, budget, 1),
    }


def capture_obligations(
    raw: Sequence[t.ProjectedAxiom], budget: Budget
) -> tuple[t.ProjectedAxiom, ...]:
    """Capture an explicit finite nonempty typed inventory before caller callbacks."""
    if not isinstance(raw, Sequence) or isinstance(cast(object, raw), str | bytes):
        raise InputError("Entailment obligations require a finite typed sequence")
    size = len(raw)
    if not size:
        raise InputError("No required entailment obligations were supplied")
    budget.consume("states", size * 4)
    budget.consume("bytes", 64 + 512 * size)
    kinds = (
        t.Declaration,
        t.SubClassOf,
        t.ClassRelation,
        t.ClassAssertion,
        t.DatatypeDefinition,
        t.PropertyDomainRange,
        t.SubPropertyOf,
        t.AnnotationAssertion,
    )
    result = []
    for index in range(size):
        budget.consume("work")
        item = raw[index]
        if type(item) not in kinds:
            raise InputError("Entailment requests require existing typed OWL axioms")
        result.append(item)
    return tuple(result)
