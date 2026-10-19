"""Closed declaration and JSON codecs for inert semantic preparation records."""

import json
from dataclasses import fields
from decimal import Decimal
from typing import cast

from rdflib import Literal, URIRef

from bijux_ontology.absence import ABSENT
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


def json_bytes(value: object, budget: Budget) -> bytes:
    """Reserve JSON traversal and worst-case encoding space before allocation."""
    budget.consume("states")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        budget.consume("work")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Preparation JSON exceeds max_depth")
        if isinstance(item, str):
            if len(item) > budget.limits.max_bytes:
                raise LimitExceeded("Preparation text exceeds max_bytes")
            budget.consume("bytes", 2 + 6 * len(item))
        elif isinstance(item, dict):
            budget.consume("states", 2 * len(item))
            budget.consume("bytes", 2 + 128 * len(item))
            pending.extend(
                (child, depth + 1) for pair in item.items() for child in pair
            )
        elif isinstance(item, (list, tuple)):
            budget.consume("states", len(item))
            budget.consume("bytes", 2 + 64 * len(item))
            pending.extend((child, depth + 1) for child in item)
        elif item is None or type(item) in (bool, int, float):
            if type(item) is int and item.bit_length() > min(
                4096, budget.limits.max_bytes
            ):
                raise LimitExceeded("Preparation integer exceeds max_bytes")
            budget.consume(
                "bytes", 32 if type(item) is not int else 32 + item.bit_length()
            )
        else:
            raise InputError("Preparation requires inert JSON data")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InputError("Preparation requires finite valid JSON") from error
    if len(payload) > budget.limits.max_bytes:
        raise LimitExceeded("Preparation artifact exceeds max_bytes")
    return payload
