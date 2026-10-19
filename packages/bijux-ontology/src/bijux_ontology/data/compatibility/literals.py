"""Actual builtin integer membership through qualified maintained witnesses."""

import pyoxigraph as ox
from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values


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
    """Qualify builtin integer membership through a maintained integer witness.

    Casting alone truncates decimals. OWL datatype-map identity against the
    original establishes whether that cast actually preserves the source value.
    Only then does the existing subtype lexical validator qualify the witness.
    """
    actual = str(value.datatype or XSD.string)
    if not actual.startswith(str(XSD)) or not str(required).startswith(str(XSD)):
        return None
    if datatype_relation(actual, str(XSD.decimal)) not in ("equal", "subset"):
        return None
    if datatype_relation(str(required), str(XSD.integer)) not in ("equal", "subset"):
        return None
    operand = str(ox.Literal(str(value), datatype=ox.NamedNode(actual)))
    query = f"SELECT (<{XSD.integer}>({operand}) AS ?value) WHERE {{}}"
    bounded_text(query, budget)
    budget.consume("resources")
    budget.consume("states", 4)
    try:
        answers = ox.Store().query(query)
        if not isinstance(answers, ox.QuerySolutions):
            return None
        rows = iter(answers)
        row = next(rows)
        budget.consume("work")
        result = row[0]
        if next(rows, None) is not None or not isinstance(result, ox.Literal):
            return None
        if result.datatype.value != str(XSD.integer):
            return None
        bounded_text(result.value, budget)
        witness = literal_term(result.value, datatype=str(XSD.integer))
        identity = compare_literal_values(
            value, witness, regime="owl2-datatype-map", budget=budget
        )
        if any(issue.kind == "exhausted" for issue in identity.report.issues):
            raise LimitExceeded("Integer witness identity exhausted its shared budget")
        if not identity.report.verified:
            return None
        if identity.relation == "different":
            return False
        if identity.relation != "equal":
            return None
        candidate = literal_term(result.value, datatype=str(required))
        validation = check_literal(candidate, budget=budget)
        if validation.verified:
            return True
        if any(issue.kind == "finding" for issue in validation.issues):
            return False
        return None
    except LimitExceeded:
        raise
    except (ValueError, RuntimeError, OverflowError, SyntaxError, StopIteration):
        return None
