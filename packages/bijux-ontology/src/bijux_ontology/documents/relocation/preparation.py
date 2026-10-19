"""Finite source-preserving context relocation without guessing term mappings."""

from collections.abc import Sequence
from dataclasses import replace
from typing import cast
from urllib.parse import urljoin

from bijux_ontology._json import owned_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.forms.types import FormRequirement
from bijux_ontology.execution.inputs import capture
from bijux_ontology.jsonld.contexts.processing import _data, _preflight
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.resources import (
    ContextResources,
    DocumentLoader,
    context_is_unscoped,
    context_resources,
)
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri


class _Unsupported(InputError):
    """Valid JSON requires context transformations outside this finite profile."""


def declarations(
    requirements: Sequence[FormRequirement],
    expectations: Sequence[ContextExpectation],
    budget: Budget,
) -> tuple[tuple[FormRequirement, ...], tuple[ContextExpectation, ...]]:
    """Own complete caller obligations before any loader can mutate their source."""
    forms = []
    for requirement in capture(requirements, budget):
        if not isinstance(requirement, FormRequirement):
            raise InputError("Relocation needs typed source-form requirements")
        _data(requirement, budget)
        forms.append(replace(requirement))
    if not forms:
        raise _Unsupported("Relocation requires selected source-form obligations")
    selected = []
    for expectation in capture(expectations, budget):
        if not isinstance(expectation, ContextExpectation):
            raise InputError("Relocation needs typed occurrence expectations")
        _data(expectation, budget)
        selected.append(replace(expectation))
    return tuple(forms), tuple(selected)


def candidate(
    document: dict[str, object], old_url: str, budget: Budget
) -> dict[str, object]:
    """Change only the root context, retaining all original data values and form.

    Local and supplied remote contexts must satisfy the existing finite unscoped
    profile. Null resets and nested data contexts are unsupported, including such
    keys in opaque payloads: no alternate context grammar interprets them here.
    Original non-context data is never searched for apparent relative IRIs.
    """
    stack: list[tuple[object, bool]] = [(document, True)]
    while stack:
        value, root = stack.pop()
        budget.consume("work")
        if isinstance(value, dict):
            if not root and "@context" in value:
                raise _Unsupported("Nested context relocation is not qualified")
            for key, child in value.items():
                if key != "@context":
                    budget.consume("states")
                    stack.append((child, False))
        elif isinstance(value, list):
            for child in value:
                budget.consume("states")
                stack.append((child, False))
    raw = document.get("@context", {})
    contexts = (
        (raw if isinstance(raw, list) else [raw]) if "@context" in document else []
    )
    budget.consume("states", len(contexts))
    budget.consume("bytes", 64 + 16 * len(contexts))
    result: list[object] = [{"@base": old_url}]
    for context in contexts:
        budget.consume("work")
        if context is None or not isinstance(context, (str, dict)):
            raise _Unsupported("Only non-null root context entries can be relocated")
        _preflight(context, budget)
        if not context_is_unscoped(context):
            raise _Unsupported("Scoped or aliased context relocation is not qualified")
        if isinstance(context, str):
            bound_text(context, budget)
            size = len(context.encode("utf-8")) + len(old_url.encode("utf-8"))
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Resolved context URL exceeds its byte profile")
            budget.consume("work", len(context) + len(old_url) + 1)
            budget.consume("bytes", 128 + 4 * (len(context) + len(old_url)))
            context = urljoin(old_url, context)
            bound_text(context, budget)
            absolute_iri(context)
        result.append(context)
    owned, _ = owned_json(document, budget)
    target = cast(dict[str, object], owned)
    target["@context"] = result
    return target


def resources(loader: DocumentLoader | None, budget: Budget) -> ContextResources:
    """Reuse one owned response capture without an implicit acquisition channel."""
    if loader is None or isinstance(loader, ContextResources):
        return context_resources(loader, budget=budget)

    def qualified(url: str, options: dict[str, object]) -> dict[str, object]:
        """Preflight returned metadata before the shared response ownership copy."""
        try:
            response = loader(url, options)
        except InputError as error:
            bound_text(str(error), budget)
            raise
        _preflight(response, budget)
        return response

    return context_resources(qualified, budget=budget)
