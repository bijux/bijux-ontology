"""Owned aggregate declarations and exact bounded integer/decimal arithmetic."""

from decimal import Decimal
from fractions import Fraction

from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.declarations.aggregates import AggregateContract
from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.terms import bound_node, literal_term


def reserve(operation: Budget, count: int = 1) -> None:
    """Reserve owned and terminal references before retaining evidence."""
    operation.consume("work", count)
    operation.consume("states", 4 * count)
    operation.consume("bytes", 256 * count)


def contract(value: AggregateContract, operation: Budget) -> AggregateContract:
    """Own all declaration fields before reading any caller record inventory."""
    if type(value) is not AggregateContract:
        raise InputError("Expected an ordinary AggregateContract")
    fields = (
        (value.name, None),
        (value.function, ("count", "distinct_count", "sum", "min", "max")),
        (value.counting, ("entities", "occurrences")),
        (value.comparison, ("equal", "at_most", "at_least")),
        (value.missing, ("reject", "exclude", "unverified")),
        (value.multiplicity, ("reject", "unverified")),
        (value.empty, ("zero", "unverified")),
    )
    reserve(operation)
    for text, choices in fields:
        if type(text) is not str:
            raise InputError("Aggregate policy fields require ordinary strings")
        bound_text(text, operation)
        if not text or text.isspace() or choices is not None and text not in choices:
            raise InputError("Choose explicit supported aggregate policies")
    path = (
        None
        if value.member_path is None
        else capture_path(value.member_path, budget=operation)
    )
    if (path is None) != (value.function == "count"):
        raise InputError(
            "Count selects records; other aggregates require a member path"
        )
    unit_path = (
        None
        if value.unit_path is None
        else capture_path(value.unit_path, budget=operation)
    )
    unit = value.unit
    if (unit is None) != (unit_path is None):
        raise InputError("A dimensional aggregate requires both unit and unit_path")
    if unit is not None:
        if type(unit) is not URIRef or value.function in ("count", "distinct_count"):
            raise InputError("Units require a numeric aggregate and an explicit IRI")
        bound_node(unit, operation)
        unit = URIRef(str(unit))
    return AggregateContract(
        value.name,
        value.function,
        value.counting,
        path,
        value.comparison,
        value.missing,
        value.multiplicity,
        value.empty,
        unit,
        unit_path,
    )


def number(value: Literal, operation: Budget, *, count: bool = False) -> Fraction:
    """Validate original RDF spelling, then use standard exact rational arithmetic.

    Supported decimal lexicals have no exponent and at most 4096 characters.
    Arithmetic storage/work is reserved before converting or multiplying integer
    coefficients. Floating point promotion and Decimal context rounding are absent.
    """
    if not isinstance(value, Literal):
        raise ResourceError("Aggregate quantities require actual RDF literals")
    bound_node(value, operation)
    raw = str(value)
    size = len(raw)
    if size > 4096 or value.language:
        raise ResourceError("Quantity is outside the exact supported numeric profile")
    required = str(XSD.integer if count else XSD.decimal)
    if datatype_relation(str(value.datatype), required) not in ("equal", "subset"):
        raise ResourceError("Quantity requires an XSD integer or exact decimal")
    operation.consume("work", 16 * (size + 1) ** 2)
    operation.consume("bytes", 64 * (size + 1))
    if not check_literal(value, budget=operation).verified:
        raise ResourceError("Quantity has an invalid or unqualified numeric lexical")
    result = Fraction(Decimal(raw))
    if count and result < 0:
        raise ResourceError("Declared counts cannot be negative")
    return result


def owned_literal(value: Literal, operation: Budget) -> Literal:
    """Capture expected bounds before callbacks or record reads can change them."""
    if type(value) is not Literal:
        raise InputError("Aggregate expected values require ordinary RDF literals")
    bound_node(value, operation)
    reserve(operation)
    return literal_term(
        str(value),
        datatype=str(value.datatype) if value.datatype else None,
        language=value.language,
    )


def arithmetic(left: Fraction, right: Fraction, operation: Budget) -> None:
    """Prepay conservative integer multiplication/comparison workspace."""
    bits = (
        sum(
            item.bit_length()
            for item in (
                left.numerator,
                left.denominator,
                right.numerator,
                right.denominator,
            )
        )
        + 8
    )
    operation.consume("work", bits * bits)
    operation.consume("bytes", 16 * bits)
