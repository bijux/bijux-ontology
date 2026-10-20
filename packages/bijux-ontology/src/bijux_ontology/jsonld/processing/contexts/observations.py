"""Observe selected source objects under their actual maintained active contexts."""

from typing import Any

from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.paths import SourcePath, at_path, bound_path
from bijux_ontology.jsonld.contexts.preparation import preflight_context_json
from bijux_ontology.jsonld.contexts.types import (
    SourceTerm,
    TermDefinition,
    UsageContextUnavailable,
)
from bijux_ontology.jsonld.processing.resources import (
    ContextInputError,
    ContextResources,
)

from .state import ContextProcessor, NativeContext, UnsupportedContext, _require_call


def source_terms(
    document: object,
    paths: tuple[SourcePath, ...],
    *,
    base: str | None,
    resources: ContextResources,
    budget: Budget,
) -> dict[SourcePath, tuple[SourceTerm, ...]]:
    """Observe exact selected object identities in one additional processor pass.

    The existing prepared document and offline resource owner are reused. No
    parent-context reconstruction or expanded-array correspondence is attempted.
    Unvisited/synthetic object scopes are omitted for the caller to qualify.
    """
    preflight_context_json(document, budget)
    for path in paths:
        bound_path(path, budget)
    try:
        bridge = ContextProcessor(resources, base, "json-ld-1.1", budget)
        _require_call(
            getattr(bridge.processor, "_expand", None),
            "source expansion",
            {},
            None,
            {},
            {},
        )
        _require_call(
            getattr(bridge.processor, "_expand_object", None),
            "source observation",
            {},
            None,
            None,
            {},
            None,
            {},
        )
    except UnsupportedContext as error:
        raise UsageContextUnavailable(str(error)) from error
    runtime = bridge.runtime
    engine = bridge.processor
    original_expand = engine._expand
    original_object = engine._expand_object
    targets: dict[int, tuple[object, SourcePath]] | None = None
    result: dict[SourcePath, tuple[SourceTerm, ...]] = {}

    def expand(
        active: Any, prop: Any, element: Any, options: Any, *args: Any, **kwargs: Any
    ) -> Any:
        """Bind selected paths once to actual objects in the processor-owned copy."""
        nonlocal targets
        budget.consume("work")
        if targets is None:
            targets = {}
            budget.consume("states", len(paths))
            for path in paths:
                budget.consume("work", len(path) + 1)
                selected = at_path(element, path)
                if isinstance(selected, dict):
                    targets[id(selected)] = (selected, path)
        return original_expand(active, prop, element, options, *args, **kwargs)

    def observe(
        active: Any,
        prop: Any,
        expanded_prop: Any,
        element: Any,
        parent: Any,
        options: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Read post-local/property/type context only for exact original objects."""
        budget.consume("work")
        assert targets is not None
        target = targets.get(id(element))
        if target is not None and target[0] is element and isinstance(element, dict):
            view = bridge.view(NativeContext(active))
            terms = []
            budget.consume("states", len(element))
            budget.consume("bytes", 64 + 64 * len(element))
            for key in element:
                budget.consume(
                    "work",
                    1 + 2 * sum(len(item.term) + len(key) + 1 for item in view.terms),
                )
                bound_text(key, budget)
                if key == "@context":
                    terms.append(
                        SourceTerm(
                            key, "@context", True, False, view.defaults.vocabulary
                        )
                    )
                    continue
                definition: TermDefinition | None = next(
                    (item for item in view.terms if item.term == key), None
                )
                iri = engine._expand_iri(active, key, vocab=True)
                if iri is not None:
                    bound_text(iri, budget)
                prefix, colon, suffix = key.partition(":")
                prefix_definition = (
                    next((item for item in view.terms if item.term == prefix), None)
                    if colon
                    else None
                )
                budget.consume("work", len(iri or "") + len(key) + 1)
                compact = bool(
                    colon
                    and prefix != "_"
                    and not suffix.startswith("//")
                    and prefix_definition is not None
                    and prefix_definition.prefix
                    and prefix_definition.iri is not None
                    and iri is not None
                    and iri.startswith(prefix_definition.iri)
                    and iri[len(prefix_definition.iri) :] == suffix
                )
                terms.append(
                    SourceTerm(
                        key,
                        iri,
                        definition is not None,
                        compact,
                        view.defaults.vocabulary,
                    )
                )
            if target[1] in result:
                raise UsageContextUnavailable(
                    "Selected source object was processed more than once"
                )
            result[target[1]] = tuple(terms)
        return original_object(
            active, prop, expanded_prop, element, parent, options, *args, **kwargs
        )

    engine._expand = expand
    engine._expand_object = observe
    try:
        engine.expand(document, bridge.options)
    except UnsupportedContext as error:
        raise UsageContextUnavailable(
            "Maintained context layout is unsupported"
        ) from error
    except runtime.JsonLdError as error:
        if bridge.failures:
            raise bridge.failures[0] from error
        raise ContextInputError(
            "Maintained source-object processing rejected the input"
        ) from error
    finally:
        engine._expand = original_expand
        engine._expand_object = original_object
    return result
