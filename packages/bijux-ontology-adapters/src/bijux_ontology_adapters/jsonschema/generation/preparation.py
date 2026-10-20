"""Bound authored output declarations and own exact RDF literal sidecar values."""

from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from sys import getsizeof

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import (
    Budget,
    bound_text,
)
from bijux_ontology.json.paths import bound_index
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import bound_node, literal_term

from .validation import capture


def bound_contract(item: object, budget: Budget, depth: int = 0) -> None:
    """Bound authored immutable metadata before retaining or formatting intent."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Output contract metadata exceeds max_depth")
    if isinstance(item, URIRef | BNode | Literal):
        bound_node(item, budget)
    elif isinstance(item, str):
        bound_text(item, budget)
    elif item is None or item is ABSENT or type(item) is bool:
        return
    elif type(item) is int:
        bound_index(item, budget)
    elif isinstance(item, Decimal):
        size = getsizeof(item)
        digits = abs(item.adjusted()) + 1
        if digits > budget.limits.max_bytes:
            raise LimitExceeded("Numeric output bounds exceed max_bytes")
        budget.consume("work", size + digits)
        budget.consume("bytes", size + 3 * digits)
    elif isinstance(item, tuple | list):
        for child in capture(item, budget):
            bound_contract(child, budget, depth + 1)
    elif is_dataclass(item) and not isinstance(item, type):
        for field in fields(item):
            bound_contract(getattr(item, field.name), budget, depth + 1)
    else:
        raise InputError("Output declarations require finite supported metadata")


def native_scalar(term: URIRef | Literal, budget: Budget) -> object:
    """Convert only exact canonical native values, never RDF value-space equality."""
    if isinstance(term, URIRef):
        return str(term)
    if not check_literal(term, budget=budget).verified or term.language is not None:
        raise InputError("Enumeration lacks exact native JSON encoding")
    if term.datatype in (None, XSD.string):
        return str(term)
    if term.datatype == XSD.boolean and str(term) in ("true", "false"):
        return str(term) == "true"
    if term.datatype == XSD.integer:
        value = term.toPython()
        if type(value) is int and str(value) == str(term):
            return value
    raise InputError("Enumeration lacks exact native JSON encoding")


def owned_literal(term: Literal, budget: Budget) -> Literal:
    """Own original lexical metadata before returning a reusable RDF mapping."""
    bound_node(term, budget)
    return literal_term(
        str(term),
        datatype=str(term.datatype) if term.datatype is not None else None,
        language=term.language,
    )


def owned_values(
    values: tuple[ValueDeclaration, ...], budget: Budget
) -> tuple[ValueDeclaration, ...]:
    """Snapshot mutable RDF literal metadata inside otherwise frozen declarations."""
    budget.consume("states", len(values))
    result = []
    for value in values:
        budget.consume("work")
        samples = tuple(
            owned_literal(term, budget) for term in capture(value.samples, budget)
        )
        constraints = value.constraints
        if constraints is not None and constraints.enumeration is not None:
            enumeration = constraints.enumeration
            terms = (
                enumeration.values
                if isinstance(enumeration, ValueEnumeration)
                else enumeration
            )
            selected = tuple(
                owned_literal(term, budget) if isinstance(term, Literal) else term
                for term in capture(terms, budget)
            )
            if isinstance(enumeration, ValueEnumeration):
                excluded = tuple(
                    owned_literal(term, budget) if isinstance(term, Literal) else term
                    for term in capture(enumeration.excluded, budget)
                )
                enumeration = replace(enumeration, values=selected, excluded=excluded)
            else:
                enumeration = selected
            constraints = replace(constraints, enumeration=enumeration)
        result.append(replace(value, samples=samples, constraints=constraints))
    return tuple(result)
