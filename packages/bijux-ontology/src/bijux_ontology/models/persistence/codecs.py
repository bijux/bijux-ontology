"""Closed declaration and JSON codecs for inert semantic preparation records."""

from dataclasses import fields
from decimal import Decimal
from typing import cast

from rdflib import Literal, URIRef

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.literal_contracts import (
    JsonLiteralObligation,
    MarkupLiteralContract,
)
from bijux_ontology.declarations.resource_roles import ResourceRole, RoleBridge
from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionCoverage,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.literal_values.json.types import JsonRequirement
from bijux_ontology.rdf.terms import bound_node

_DECLARATIONS = (
    ModelDeclaration,
    FieldDeclaration,
    ValueDeclaration,
    ValueConstraints,
    ResourceRole,
    RoleBridge,
    EmissionCondition,
    EmissionCoverage,
    EmissionGroup,
    EmissionScenario,
    OutputRequirement,
    SubjectIdentity,
    JsonRequirement,
)


def declaration_data(value: object, budget: Budget, depth: int = 0) -> object:
    """Encode only the supported declaration schema, preserving exact RDF terms."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Preparation declaration exceeds max_depth")
    if value is ABSENT:
        return {"absent": True}
    if isinstance(value, Literal):
        bound_node(value, budget)
        return {
            "lexical": str(value),
            "datatype": (
                str(value.datatype)
                if value.datatype is not None and not value.language
                else None
            ),
            "language": value.language,
        }
    if isinstance(value, URIRef):
        bound_node(value, budget)
        return {"iri": str(value)}
    if isinstance(value, Decimal):
        size = value.__sizeof__()
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Preparation decimal exceeds max_bytes")
        # Native coefficient storage understates Python's digit-vector workspace.
        # Pay conversion storage and traversal before requesting that vector.
        budget.consume("bytes", 32 * size)
        budget.consume("work", 3 * size)
        parts = value.as_tuple()
        exponent = parts.exponent
        if not isinstance(exponent, int):
            raise InputError("Preparation requires finite decimal bounds")
        length = len(parts.digits) + abs(exponent) + 8
        if length > budget.limits.max_bytes:
            raise LimitExceeded("Preparation decimal exceeds max_bytes")
        budget.consume("bytes", 4 * length)
        budget.consume("work", length)
        return format(value, "f")
    if isinstance(value, JsonLiteralObligation):
        contract = value.contract(budget)
        return {
            "kind": "json",
            "requirements": declaration_data(contract.requirements, budget, depth + 1),
            "schema": contract.schema,
            "dialect": contract.dialect,
        }
    if isinstance(value, MarkupLiteralContract):
        return {
            "kind": "markup",
            "canonicalization": value.canonicalization,
            "expected": declaration_data(value.expected, budget, depth + 1),
        }
    if type(value) in _DECLARATIONS:
        budget.consume("bytes", 64 + 32 * len(fields(cast(ModelDeclaration, value))))
        return {
            field.name: declaration_data(getattr(value, field.name), budget, depth + 1)
            for field in fields(cast(ModelDeclaration, value))
        }
    if isinstance(value, tuple):
        budget.consume("bytes", 64 + 8 * len(value))
        return [declaration_data(item, budget, depth + 1) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    raise InputError(
        "Preparation does not support live enumerations or declaration extensions"
    )
