"""Bounded exact integer membership after original RDF lexical qualification."""

from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.literals import (
    check_literal,
    datatype_relation,
    integer_interval,
)


def bounded_text(value: str, budget: Budget) -> str:
    """Qualify owned diagnostic/query text before encoding or retention."""
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Data compatibility text exceeds max_bytes")
    budget.consume("work", len(value) + 1)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Data compatibility requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Data compatibility text exceeds max_bytes")
    budget.consume("bytes", size)
    return value


def integer_membership(value: Literal, required: URIRef, budget: Budget) -> bool | None:
    """Check exact integer membership after qualifying the original RDF lexical.

    Supported integer/decimal lexicals have no exponent and at most 4096
    characters. Only an all-zero fractional part has an integer value; the
    established integer interval then decides membership without a cast query
    or a discarded witness finding. Original RDF term identity is unchanged.
    """
    actual = str(value.datatype or XSD.string)
    interval = integer_interval(str(required))
    if interval is None or datatype_relation(actual, str(XSD.decimal)) not in (
        "equal",
        "subset",
    ):
        return None
    validation = check_literal(value, budget=budget)
    if any(issue.kind == "exhausted" for issue in validation.issues):
        raise LimitExceeded("Integer membership exhausted original lexical validation")
    if not validation.verified:
        return None
    raw = str(value)
    size = len(raw)
    # Original validation bounds the coefficient. Pay its slices and exact
    # integer conversion before allocating either; no Decimal context is used.
    budget.consume("work", (size + 1) ** 2)
    budget.consume("bytes", 256 + 16 * (size + 1))
    budget.consume("states", 4)
    whole, _, fractional = raw.partition(".")
    if any(digit != "0" for digit in fractional):
        return False
    if whole in ("", "+", "-"):
        whole += "0"
    number = int(whole)
    low, high = interval
    return (low is None or number >= low) and (high is None or number <= high)
