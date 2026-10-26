"""Expand actual JSON-LD with controlled resources and retained omission evidence."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.processing.resources import (
    ContextInputError,
    ContextResources,
    DocumentLoader,
    _consume_work,
    _json_size,
    context_resources,
)
from bijux_ontology.jsonld.processing.runtime import document_processor, processor
from bijux_ontology.rdf.identifiers import absolute_iri

_EXPANSION_OPTIONS: tuple[tuple[str, str | bool], ...] = (
    ("processingMode", "json-ld-1.1"),
    ("isFrame", False),
    ("keepFreeFloatingNodes", False),
    ("extractAllScripts", False),
)


@dataclass(frozen=True, init=False, slots=True, eq=False, repr=False)
class OriginalExpansion:
    """Private owned input, fixed native options and one actual original expansion.

    No public mutable document or expanded-data export is provided. Concrete
    processing consumers share this operation receipt, never a cached verdict.
    """

    _document: object
    _base: str | None
    _resources: ContextResources
    _budget: Budget
    _options: tuple[tuple[str, str | bool], ...]
    _expanded: list[dict[str, object]]
    _dropped: tuple[str | None, ...]

    def __init__(self) -> None:
        """Require actual bounded capture and maintained processing."""
        raise InputError("Use capture_original_expansion to own an expansion")

    @classmethod
    def _new(
        cls,
        document: object,
        base: str | None,
        resources: ContextResources,
        expanded: list[dict[str, object]],
        dropped: tuple[str | None, ...],
    ) -> "OriginalExpansion":
        """Publish only complete processing under this exact resource operation."""
        value = object.__new__(cls)
        object.__setattr__(value, "_document", document)
        object.__setattr__(value, "_base", base)
        object.__setattr__(value, "_resources", resources)
        object.__setattr__(value, "_budget", resources.budget)
        object.__setattr__(value, "_options", _EXPANSION_OPTIONS)
        object.__setattr__(value, "_expanded", expanded)
        object.__setattr__(value, "_dropped", dropped)
        return value

    def require_operation(self, budget: Budget) -> None:
        """Reject budget/resource rebinding and incompatible processing options."""
        if budget is not self._budget or self._resources.budget is not budget:
            raise InputError("Original expansion requires its operation budget")
        if self._options != _EXPANSION_OPTIONS:
            raise InputError("Original expansion uses different processing options")


def capture_original_expansion(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    on_property_dropped: Callable[[str | None], None] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OriginalExpansion:
    """Expand JSON-LD without global loader changes or implicit network access.

    The caller supplies every referenced document. Programmer failures in a
    custom loader propagate rather than becoming successful empty expansions.
    """
    jsonld = processor()
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    operation.consume("states", 8)
    operation.consume("bytes", 192)
    document = own_json(
        document,
        max_bytes=operation.limits.max_bytes,
        max_depth=operation.limits.max_depth,
        budget=operation,
    )
    operation.consume("bytes", _json_size(document))
    _consume_work(document, operation)
    if base is not None:
        absolute_iri(base)
    loader_failure: list[Exception] = []
    callback_failure: list[Exception] = []
    omitted: list[str | None] = []

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
        operation.consume("states")
        operation.consume("bytes", 16)
        omitted.append(term)
        if on_property_dropped is not None:
            try:
                on_property_dropped(term)
            except Exception as error:
                callback_failure.append(error)
                raise

    options: dict[str, object] = {
        **dict(_EXPANSION_OPTIONS),
        "documentLoader": load,
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
    return OriginalExpansion._new(
        document,
        base,
        resources,
        cast(list[dict[str, object]], expanded),
        tuple(omitted),
    )


def expand_document(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    on_property_dropped: Callable[[str | None], None] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> list[dict[str, object]]:
    """Expand fresh caller input without implicit retrieval or shared mutable data.

    Loader and dropped-property callbacks retain their own failure identities.
    The returned list belongs only to this standalone public processing call.
    """
    return capture_original_expansion(
        document,
        base=base,
        document_loader=document_loader,
        on_property_dropped=on_property_dropped,
        limits=limits,
        budget=budget,
    )._expanded


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
