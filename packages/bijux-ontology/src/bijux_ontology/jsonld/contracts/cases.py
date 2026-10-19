"""Representative JSON-LD case ownership shared by composition and publication."""

import json
from dataclasses import dataclass, replace
from typing import cast

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.jsonld.contexts.preparation import (
    capture_context_json,
    capture_selections,
    context_data,
    encode_context_json,
)
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation


@dataclass(frozen=True)
class ContextCase:
    """One required representative object without its own root @context.

    Nested contexts remain part of the actual sample and are processed normally.
    Expectations describe required exercised behavior, not all future documents.
    Composition captures every input before any supplied loader is called.
    """

    name: str
    document: object
    expectations: tuple[ContextExpectation, ...]


@dataclass(frozen=True)
class PreparedContextCase:
    """Operation-owned immutable representative input and typed expectations."""

    name: str
    document_json: str
    expectations: tuple[ContextExpectation, ...]


def capture_case(case: ContextCase, budget: Budget) -> PreparedContextCase:
    """Qualify every field before copying, hashing or invoking a resource loader."""
    if not isinstance(case, ContextCase):
        raise InputError("Retained contracts require typed ContextCase records")
    bound_text(case.name, budget)
    if not case.name:
        raise InputError("Retained cases require nonempty names")
    document = capture_context_json(case.document, budget)
    if not isinstance(document, dict) or "@context" in document:
        raise InputError("Retained case roots must be objects without @context")
    expectations = capture_selections(case.expectations, budget)
    if not expectations:
        raise InputError("Each retained case requires actual usage expectations")
    captured_expectations = []
    for expectation in expectations:
        if not isinstance(expectation, ContextExpectation):
            raise InputError("Expected typed ContextExpectation obligations")
        context_data(expectation, budget)
        budget.consume("states")
        budget.consume("bytes", 64)
        captured_expectations.append(replace(expectation))
    return PreparedContextCase(
        str(case.name),
        encode_context_json(document, budget),
        tuple(captured_expectations),
    )


def bind_case(
    case: PreparedContextCase, context: object, budget: Budget
) -> dict[str, object]:
    """Bind the selected context to the same bounded representative object."""
    document = json.loads(case.document_json)
    budget.consume("states")
    budget.consume("bytes", 32)
    document["@context"] = context
    return cast(dict[str, object], document)
