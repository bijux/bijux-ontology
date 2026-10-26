"""Qualified finite literal comparisons without rewriting RDF terms."""

from __future__ import annotations

from rdflib import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry

from .observations import ScalarComparisons
from .types import ComparisonRegime, LiteralValueComparison


def compare_literal_values(
    left: Literal,
    right: Literal,
    *,
    regime: ComparisonRegime,
    datatype_registry: DatatypeRegistry | None = None,
    budget: Budget | None = None,
) -> LiteralValueComparison:
    """Compare supported literal values while retaining both original RDF terms.

    SPARQL promotes numeric operands. OWL2 datatype-map identity preserves disjoint
    decimal/float/double families, signed zeros and time-zone offsets. Supported
    calendars are explicitly timezoned dateTime (plus OWL dateTimeStamp), years
    1..9999 and
    at most six fractional digits, using the optional maintained XSD backend.
    NaN and custom comparison remain unknown; caller conversion is not equality.
    Bounds cover input/comparison work and the fixed numeric expression, with no
    preemption claim for ordinary maintained in-process scalar operations.
    """
    if not isinstance(left, Literal) or not isinstance(right, Literal):
        raise InputError("Value comparison requires two RDF literals")
    if regime not in ("sparql-1.1", "owl2-datatype-map"):
        raise InputError("Select an explicit supported literal comparison regime")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected a DatatypeRegistry")
    operation = budget or Budget()
    return ScalarComparisons(operation).compare(
        left, right, regime=regime, datatype_registry=datatype_registry
    )
