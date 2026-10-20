"""Expand actual JSON-LD with controlled resources and retained omission evidence."""

from collections.abc import Callable, Iterator, Sequence
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.processing.resources import (
    ContextInputError,
    DocumentLoader,
    _consume_work,
    _json_size,
    context_resources,
)
from bijux_ontology.jsonld.processing.runtime import document_processor, processor
from bijux_ontology.rdf.identifiers import absolute_iri


def expand_document(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    on_property_dropped: Callable[[str | None], None] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> list[dict[str, object]]:
    """Expand JSON-LD without global loader changes or implicit network access.

    The caller supplies every referenced document. Programmer failures in a
    custom loader propagate rather than becoming successful empty expansions.
    """
    jsonld = processor()
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    document = own_json(
        document,
        max_bytes=operation.limits.max_bytes,
        max_depth=operation.limits.max_depth,
    )
    operation.consume("bytes", _json_size(document))
    _consume_work(document, operation)
    if base is not None:
        absolute_iri(base)
    loader_failure: list[Exception] = []
    callback_failure: list[Exception] = []

    def load(url: str, options: dict[str, object]) -> dict[str, object]:
        """Isolate loader-owned data and preserve unexpected caller failures."""
        try:
            response = resources(url, options)
        except Exception as error:
            loader_failure.append(error)
            raise
        return response

    def dropped(term: str | None) -> None:
        """Keep consumer callback failures distinct from native processing defects."""
        if on_property_dropped is not None:
            try:
                on_property_dropped(term)
            except Exception as error:
                callback_failure.append(error)
                raise

    options: dict[str, object] = {
        "documentLoader": load,
        "processingMode": "json-ld-1.1",
        "contextResolver": jsonld.ContextResolver(
            {}, load, max_context_urls=operation.limits.max_resources
        ),
    }
    if base is not None:
        options["base"] = base
    try:
        expanded: object = document_processor(on_property_dropped=dropped).expand(
            document, options
        )
    except (
        jsonld.JsonLdError,
        RecursionError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        if callback_failure:
            if callback_failure[0] is error:
                raise
            raise callback_failure[0] from error
        if loader_failure:
            failure = loader_failure[0]
            if isinstance(failure, (ContextInputError, OSError)):
                raise ContextInputError(str(failure)) from failure
            if not isinstance(failure, jsonld.JsonLdError):
                if failure is error:
                    raise
                raise failure from error
        if isinstance(error, (TypeError, ValueError)):
            raise ResourceError(
                "Maintained JSON-LD expansion failed without a semantic input "
                "diagnostic: " + str(error)
            ) from error
        if isinstance(error, (RecursionError, OverflowError)):
            raise LimitExceeded(f"JSON-LD processor range exceeded: {error}") from error
        raise ContextInputError(str(error)) from error
    if not isinstance(expanded, list) or any(
        not isinstance(node, dict) for node in expanded
    ):
        raise ContextInputError("JSON-LD expansion returned an unexpected result")
    return cast(list[dict[str, object]], expanded)


def _validate_allowed_drops(values: frozenset[str]) -> None:
    if not isinstance(values, frozenset) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise InputError("allowed_drops requires exact nonempty observed term strings")


def _drop_issues(
    dropped: Sequence[str | None], allowed: frozenset[str]
) -> Iterator[Issue]:
    return (
        Issue(
            "context.property_omitted"
            if term in allowed
            else "context.property_dropped",
            "The explicit omission policy excludes this observed term"
            if term in allowed
            else "A property was omitted from the supplied JSON-LD document",
            severity="info" if term in allowed else "error",
            actual=term,
        )
        for term in dict.fromkeys(dropped)
    )
