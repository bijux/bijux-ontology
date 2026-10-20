"""Apply selected opaque literal obligations through maintained evaluators."""

from dataclasses import replace

from rdflib import Literal

from bijux_ontology.declarations.literal_contracts import (
    JsonLiteralObligation,
    MarkupLiteralContract,
)
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literal_values.json.checking import check_json_literal
from bijux_ontology.rdf.literal_values.json.types import JsonLiteralContract
from bijux_ontology.rdf.literal_values.markup.checking import (
    compare_markup_values,
    inspect_markup_literal,
)
from bijux_ontology.rdf.literals.checking import check_literal


def _located(report: Report, where: str, budget: Budget) -> Report:
    """Keep selected native evidence at its original owning application field."""
    budget.consume("work", len(report.issues) + len(report.children) + 1)
    budget.consume("states", len(report.issues) + len(report.children) + 1)
    budget.consume("bytes", 128 + 128 * len(report.issues) + 64 * len(report.children))
    return replace(
        report,
        issues=tuple(replace(issue, location=where) for issue in report.issues),
        children=tuple(_located(child, where, budget) for child in report.children),
    )


def check_field_literal(
    literal: Literal,
    contract: JsonLiteralContract
    | JsonLiteralObligation
    | MarkupLiteralContract
    | None,
    *,
    where: str,
    budget: Budget,
    datatype_registry: DatatypeRegistry | None = None,
) -> Report:
    """Constrain opaque values without interpreting their content as RDF edges."""
    if isinstance(contract, JsonLiteralContract):
        contract = JsonLiteralObligation(contract, budget=budget)
    if isinstance(contract, JsonLiteralObligation):
        return check_json_literal(
            literal, contract.contract(budget), location=where, budget=budget
        ).report
    if isinstance(contract, MarkupLiteralContract):
        if contract.expected is not None and contract.canonicalization is not None:
            report = compare_markup_values(
                literal,
                contract.expected,
                regime=contract.canonicalization,
                budget=budget,
            ).report
        else:
            report = inspect_markup_literal(
                literal,
                canonicalization=contract.canonicalization,
                budget=budget,
            ).report
        return _located(report, where, budget)
    return check_literal(
        literal, location=where, budget=budget, datatype_registry=datatype_registry
    )
