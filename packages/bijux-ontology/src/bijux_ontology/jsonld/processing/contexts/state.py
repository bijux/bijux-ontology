"""Qualify maintained PyLD context state and actual source-object observations."""

from collections.abc import Mapping
from dataclasses import dataclass
from inspect import signature
from typing import Any, cast

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.jsonld.contexts.preparation import (
    capture_selections,
    encode_context_json,
    preflight_context_json,
)
from bijux_ontology.jsonld.contexts.types import (
    ContextDefaults,
    ContextScope,
    ContextView,
    TermDefinition,
)
from bijux_ontology.jsonld.processing.resources import (
    ContextInputError,
    ContextResources,
    DocumentLoader,
    context_resources,
)
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri

_ACTIVE = frozenset(
    (
        "mappings",
        "processingMode",
        "@base",
        "@vocab",
        "@language",
        "@direction",
        "@version",
        "previousContext",
        "_uuid",
    )
)
_TERM = frozenset(
    (
        "@id",
        "@type",
        "@container",
        "@index",
        "@language",
        "@direction",
        "@nest",
        "@context",
        "reverse",
        "protected",
        "_prefix",
        "_term_has_colon",
    )
)


class UnsupportedContext(InputError):
    """The installed private layout or selected transition is not qualified."""


def _require_call(method: Any, name: str, *args: Any, **kwargs: Any) -> None:
    """Qualify only the maintained call shapes used by this adapter."""
    try:
        signature(method).bind(*args, **kwargs)
    except (TypeError, ValueError) as error:
        raise UnsupportedContext(
            f"Unsupported PyLD {name}; use bijux-ontology[jsonld] "
            "with a compatible runtime"
        ) from error


def _layout(
    value: object, allowed: frozenset[str], budget: Budget
) -> Mapping[str, Any]:
    """Reject unknown private structures instead of guessing effective semantics."""
    if not isinstance(value, Mapping):
        raise UnsupportedContext("Unexpected processed context structure")
    budget.consume("states", len(value))
    for key in value:
        bound_text(key, budget)
        if key not in allowed:
            raise UnsupportedContext("Unknown processed context field")
    return cast(Mapping[str, Any], value)


def _text(
    value: object, budget: Budget, *, nullable: bool = False, absent: bool = False
) -> Any:
    """Qualify processed scalar fields before exposing or comparing them."""
    if value is ABSENT and absent or value is None and nullable:
        return value
    if not isinstance(value, str):
        raise UnsupportedContext("Unexpected processed context scalar")
    bound_text(value, budget)
    return str(value)


@dataclass(frozen=True)
class NativeContext:
    """Keep the maintained active state opaque outside its processing adapter."""

    _value: Any


class ContextProcessor:
    """Own the supported private processor interaction for a single operation."""

    def __init__(
        self,
        loader: DocumentLoader | None,
        base: str | None,
        mode: str,
        budget: Budget,
    ) -> None:
        """Use fresh resolver state and existing offline resource ownership."""
        self.runtime = runtime = processor()
        _require_call(getattr(runtime, "JsonLdProcessor", None), "processor")
        self.processor = runtime.JsonLdProcessor()
        _require_call(
            getattr(self.processor, "_get_initial_context", None), "initial context", {}
        )
        _require_call(
            getattr(self.processor, "_process_context", None),
            "context processing",
            {},
            {},
            {},
            override_protected=False,
            propagate=True,
        )
        _require_call(
            getattr(self.processor, "_expand_iri", None),
            "IRI expansion",
            {},
            "",
            base="",
            vocab=True,
        )
        _require_call(
            getattr(self.processor, "get_context_value", None),
            "context value",
            {},
            "",
            "@type",
        )
        _require_call(
            getattr(runtime, "ContextResolver", None),
            "context resolver",
            {},
            None,
            max_context_urls=1,
        )
        self.budget = budget
        self.base = base
        self.failures: list[Exception] = []
        if isinstance(loader, ContextResources):
            resources = context_resources(loader, budget=budget)
        else:

            def supplied(url: str, options: dict[str, object]) -> dict[str, object]:
                """Bound caller response metadata before the shared resource layer."""
                if loader is None:
                    raise ContextInputError("No supplied context resource")
                response = loader(url, options)
                preflight_context_json(response, budget)
                return response

            resources = context_resources(supplied, budget=budget)

        def load(url: str, options: dict[str, object]) -> dict[str, object]:
            """Bound logical identities and retain original loader failures."""
            bound_text(url, budget)
            try:
                return resources(url, options)
            except Exception as error:
                if not self.failures:
                    self.failures.append(error)
                raise

        self.options = {
            "processingMode": mode,
            "base": base,
            "documentLoader": load,
            "contextResolver": runtime.ContextResolver(
                {}, load, max_context_urls=budget.limits.max_resources
            ),
        }

    def initial(self) -> NativeContext:
        """Obtain the maintained initial context without exposing its dictionary."""
        return NativeContext(self.processor._get_initial_context(self.options))

    def selected_scope(
        self, active: NativeContext, selection: ContextScope
    ) -> NativeContext | None:
        """Apply an actual selected definition; absence is the caller's policy."""
        definition = active._value["mappings"].get(selection.term)
        if not isinstance(definition, Mapping) or "@context" not in definition:
            return None
        return self.apply(active, definition["@context"], scope=selection)

    def apply(
        self, active: NativeContext, local: object, *, scope: ContextScope | None = None
    ) -> NativeContext:
        """Invoke maintained processing, including its protected-nullification check."""
        self.budget.consume("work")
        # Empty arrays take a processor shortcut which omits processingMode.
        # The equivalent empty object uses its maintained no-op initialization.
        if type(local) is list and not local:
            local = {}
        try:
            # The public process_context(None) shortcut skips protected checks.
            # Expansion uses this maintained algorithm, including null contexts.
            return NativeContext(
                self.processor._process_context(
                    active._value,
                    local,
                    self.options,
                    override_protected=scope is not None and scope.kind == "property",
                    propagate=scope is None or scope.kind == "property",
                )
            )
        except self.runtime.JsonLdError as error:
            if self.failures:
                raise self.failures[0] from error
            code = getattr(error, "code", None)
            if isinstance(code, str):
                bound_text(code, self.budget)
            failure = ContextInputError(
                "Maintained context processing rejected the input"
            )
            failure.processor_code = code  # type: ignore[attr-defined]
            raise failure from error

    def term(self, active: NativeContext, name: str, mapping: object) -> TermDefinition:
        """Project flags and coercions without interpreting raw context grammar."""
        entry = _layout(mapping, _TERM, self.budget) if mapping is not None else {}

        def text(key: str) -> Any:
            """Read a supported processed override without losing explicit null."""
            return _text(
                entry.get(key, ABSENT),
                self.budget,
                absent=True,
                nullable=key in ("@language", "@direction"),
            )

        flags = []
        for key in ("reverse", "_prefix", "protected"):
            value = entry.get(key, False)
            if type(value) is not bool:
                raise UnsupportedContext("Unexpected processed context flag")
            flags.append(value)
        container = capture_selections(entry.get("@container", ()), self.budget)
        for value in container:
            _text(value, self.budget)
        containers = tuple(sorted(set(container)))
        index: str | Absent = ABSENT
        index_type = index_language = index_direction = ABSENT
        if "@index" in containers:
            index_key = entry.get("@index", "@index")
            _text(index_key, self.budget)
            if index_key == "@index":
                index = "@index"
            else:
                # Match the maintained map expansion's actual IRI/coercion lookup.
                index = self.processor._expand_iri(
                    active._value, index_key, vocab=self.options.get("base", "")
                )
                _text(index, self.budget)
                try:
                    absolute_iri(cast(str, index))
                except InputError as error:
                    raise UnsupportedContext(
                        "Custom index property has no resolved IRI"
                    ) from error
                index_type = self.runtime.JsonLdProcessor.get_context_value(
                    active._value, index_key, "@type"
                )
                index_language = self.runtime.JsonLdProcessor.get_context_value(
                    active._value, index_key, "@language"
                )
                index_direction = self.runtime.JsonLdProcessor.get_context_value(
                    active._value, index_key, "@direction"
                )
                for item in (index_type, index_language, index_direction):
                    _text(item, self.budget, nullable=True)
        raw_scope = entry.get("@context", ABSENT)
        scope_json = (
            None
            if raw_scope is ABSENT
            else encode_context_json(
                None if raw_scope is False else raw_scope, self.budget
            )
        )
        language = self.runtime.JsonLdProcessor.get_context_value(
            active._value, name, "@language"
        )
        direction = self.runtime.JsonLdProcessor.get_context_value(
            active._value, name, "@direction"
        )
        _text(language, self.budget, nullable=True)
        _text(direction, self.budget, nullable=True)
        nest = text("@nest")
        if nest is not ABSENT:
            nest = self.processor._expand_iri(active._value, nest, vocab=True)
            if nest != "@nest":
                raise UnsupportedContext("Unresolved nest keyword alias")
        self.budget.consume("states")
        self.budget.consume("bytes", 512)
        return TermDefinition(
            name,
            _text(entry.get("@id"), self.budget, nullable=True),
            flags[0],
            text("@type"),
            containers,
            index,
            index_type,
            index_language,
            index_direction,
            text("@language"),
            text("@direction"),
            language,
            direction,
            flags[1],
            flags[2],
            nest,
            scope_json,
        )

    def view(
        self, active: NativeContext, depth: int = 0, seen: frozenset[int] = frozenset()
    ) -> ContextView:
        """Capture only the qualified processor layout with bounded reversion state."""
        self.budget.consume("work")
        self.budget.consume("states")
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("Processed context reversion exceeds max_depth")
        if id(active._value) in seen:
            raise UnsupportedContext("Cyclic processed context reversion")
        state = _layout(active._value, _ACTIVE, self.budget)
        mode = _text(state.get("processingMode"), self.budget)
        base_mapping = _text(
            state.get("@base", ABSENT), self.budget, nullable=True, absent=True
        )
        try:
            effective_base = (
                self.processor._expand_iri(
                    active._value, "", base=self.base if self.base is not None else ""
                )
                if base_mapping is not ABSENT
                else self.base
            )
        except ValueError as error:
            raise UnsupportedContext(
                "Native base resolution is not qualified"
            ) from error
        _text(effective_base, self.budget, nullable=True)
        self.budget.consume("bytes", 256)
        defaults = ContextDefaults(
            mode,
            self.base,
            base_mapping,
            effective_base or None,
            _text(state.get("@vocab"), self.budget, nullable=True),
            _text(state.get("@language"), self.budget, nullable=True),
            _text(state.get("@direction"), self.budget, nullable=True),
        )
        mappings = state.get("mappings")
        if not isinstance(mappings, Mapping):
            raise UnsupportedContext("Missing processed term inventory")
        self.budget.consume("states", len(mappings))
        names = []
        for name in mappings:
            _text(name, self.budget)
            names.append(name)
        self.budget.consume(
            "work",
            sum(len(name) + 1 for name in names) * max(1, len(names).bit_length()),
        )
        terms = tuple(self.term(active, name, mappings[name]) for name in sorted(names))
        previous = state.get("previousContext")
        prior = (
            self.view(NativeContext(previous), depth + 1, seen | {id(active._value)})
            if previous is not None
            else None
        )
        return ContextView(defaults, terms, prior)
