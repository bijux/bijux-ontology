"""Maintained framing with owned JSON and one offline resource operation."""

from bijux_ontology._json import own_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.resources import (
    ContextInputError,
    ContextResources,
)
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text


def own_document(value: object, resources: ContextResources) -> object:
    """Bound mutable input and count its semantic payload before processor use."""
    budget = resources.budget
    return own_json(
        value,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
        budget=budget,
    )


def process_frame(
    document: object,
    frame: object,
    resources: ContextResources,
    base: str | None,
) -> object:
    """Use PyLD's actual matching/embedding algorithm without loader globals.

    Custom loaders retain ordinary in-process execution. Unexpected loader
    failures propagate, while selected resource and processor limits remain
    distinguishable from malformed framing input.
    """
    budget = resources.budget
    jsonld = processor()
    if base is not None:
        bound_text(base, budget)
        absolute_iri(base)
    if not isinstance(frame, (str, dict)):
        raise InputError("A JSON-LD frame requires an object or supplied document URL")
    failures: list[Exception] = []

    def load(url: str, options: dict[str, object]) -> dict[str, object]:
        """Reuse owned resources and retain the original caller-failure boundary."""
        try:
            return resources(url, options)
        except Exception as error:
            if not failures:
                failures.append(error)
            raise

    options: dict[str, object] = {
        "processingMode": "json-ld-1.1",
        "documentLoader": load,
        "contextResolver": jsonld.ContextResolver(
            {}, load, max_context_urls=budget.limits.max_resources
        ),
    }
    if base is not None or not isinstance(document, str):
        options["base"] = base
    try:
        result: object = jsonld.frame(document, frame, options=options)
    except (jsonld.JsonLdError, RecursionError, OverflowError) as error:
        if failures:
            failure = failures[0]
            if isinstance(failure, (LimitExceeded, InputError)):
                raise failure from error
            if isinstance(failure, OSError):
                raise ContextInputError(
                    "A supplied framing resource is unavailable"
                ) from failure
            if not isinstance(failure, jsonld.JsonLdError):
                raise failure from error
        if isinstance(error, (RecursionError, OverflowError)):
            raise LimitExceeded(
                "The maintained framing processor exceeded its range"
            ) from error
        raise ContextInputError(
            "The maintained JSON-LD processor rejected framing"
        ) from error
    return own_document(result, resources)
