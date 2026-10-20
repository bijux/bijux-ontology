"""Bounded adapter from maintained processor state to owned context records."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from math import isfinite
from typing import Any, TypeVar, cast

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import bound_index
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.processing.resources import (
    ContextInputError,
    ContextResources,
    DocumentLoader,
    context_resources,
)
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import (
    ContextDefaults,
    ContextInspection,
    ContextScope,
    ContextView,
    TermDefinition,
)

T = TypeVar("T")
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


class _Unsupported(InputError):
    """The installed private layout or selected transition is not qualified."""


def _capture(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    """Capture indexed finite inventories without trusting an arbitrary iterator."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise InputError("Context selections require finite sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            result.append(values[index])
        except IndexError as error:
            raise InputError("Context selection changed during capture") from error
    return tuple(result)


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Keep explicit limit settings aligned with one cumulative operation."""
    if budget is not None and (
        not isinstance(budget, Budget) or limits is not None and limits != budget.limits
    ):
        raise InputError("Use a shared Budget with matching limits")
    return budget if budget is not None else Budget(limits)


def _preflight(value: object, budget: Budget, depth: int = 0) -> int:
    """Charge JSON metadata before copies, hashing, or processor callbacks."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Context metadata exceeds max_depth")
    if isinstance(value, str):
        bound_text(value, budget)
        return 1
    if value is None or type(value) is bool:
        return 1
    if type(value) is int:
        bound_index(value, budget)
        return 1
    if type(value) is float and isfinite(value):
        bound_text(repr(value), budget)
        return 1
    if type(value) is list:
        return 1 + sum(
            _preflight(item, budget, depth + 1) for item in _capture(value, budget)
        )
    if type(value) is dict:
        budget.consume("states", len(value))
        total = 1
        for key, child in value.items():
            bound_text(key, budget)
            total += _preflight(child, budget, depth + 1)
        return total
    raise InputError("Contexts require finite JSON values")


def _own(value: object, budget: Budget) -> object:
    """Own qualified JSON without preserving mutable caller dictionaries."""
    _preflight(value, budget)
    return own_json(
        value, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    )


def _data(value: object, budget: Budget, depth: int = 0) -> object:
    """Encode only the finite immutable records, preserving explicit absence."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Context records exceed max_depth")
    if value is ABSENT:
        return {"absent": True}
    if is_dataclass(value) and not isinstance(value, type):
        names = fields(value)
        budget.consume("states", len(names))
        return {
            field.name: _data(getattr(value, field.name), budget, depth + 1)
            for field in names
        }
    if isinstance(value, tuple):
        budget.consume("states", len(value))
        return [_data(item, budget, depth + 1) for item in value]
    _preflight(value, budget, depth)
    return value


def _dump(value: object, budget: Budget) -> str:
    """Store bounded independent transport data using the existing JSON boundary."""
    owned = _own(value, budget)
    result = json.dumps(
        owned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    bound_text(result, budget)
    return result


def _layout(
    value: object, allowed: frozenset[str], budget: Budget
) -> Mapping[str, Any]:
    """Reject unknown private structures instead of guessing effective semantics."""
    if not isinstance(value, Mapping):
        raise _Unsupported("Unexpected processed context structure")
    budget.consume("states", len(value))
    for key in value:
        bound_text(key, budget)
        if key not in allowed:
            raise _Unsupported("Unknown processed context field")
    return cast(Mapping[str, Any], value)


def _text(
    value: object, budget: Budget, *, nullable: bool = False, absent: bool = False
) -> Any:
    """Qualify processed scalar fields before exposing or comparing them."""
    if value is ABSENT and absent or value is None and nullable:
        return value
    if not isinstance(value, str):
        raise _Unsupported("Unexpected processed context scalar")
    bound_text(value, budget)
    return str(value)


class _Bridge:
    """Own the supported private processor interaction for a single operation."""

    def __init__(
        self,
        runtime: Any,
        loader: DocumentLoader | None,
        base: str | None,
        mode: str,
        budget: Budget,
    ) -> None:
        """Use fresh resolver state and existing offline resource ownership."""
        self.runtime = runtime
        if not callable(getattr(runtime, "JsonLdProcessor", None)) or not callable(
            getattr(runtime, "ContextResolver", None)
        ):
            raise _Unsupported("Unsupported maintained processor entry point")
        self.processor = runtime.JsonLdProcessor()
        if any(
            not callable(getattr(self.processor, name, None))
            for name in ("_get_initial_context", "_process_context", "_expand_iri")
        ):
            raise _Unsupported("Unsupported maintained processor methods")
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
                _preflight(response, budget)
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

    def apply(
        self, active: Any, local: object, *, scope: ContextScope | None = None
    ) -> Any:
        """Invoke maintained processing, including its protected-nullification check."""
        self.budget.consume("work")
        # Empty arrays take a processor shortcut which omits processingMode.
        # The equivalent empty object uses its maintained no-op initialization.
        if type(local) is list and not local:
            local = {}
        try:
            # The public process_context(None) shortcut skips protected checks.
            # Expansion uses this maintained algorithm, including null contexts.
            return self.processor._process_context(
                active,
                local,
                self.options,
                override_protected=scope is not None and scope.kind == "property",
                propagate=scope is None or scope.kind == "property",
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

    def term(self, active: Any, name: str, mapping: object) -> TermDefinition:
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
                raise _Unsupported("Unexpected processed context flag")
            flags.append(value)
        container = _capture(entry.get("@container", ()), self.budget)
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
                    active, index_key, vocab=self.options.get("base", "")
                )
                _text(index, self.budget)
                try:
                    absolute_iri(cast(str, index))
                except InputError as error:
                    raise _Unsupported(
                        "Custom index property has no resolved IRI"
                    ) from error
                index_type = self.runtime.JsonLdProcessor.get_context_value(
                    active, index_key, "@type"
                )
                index_language = self.runtime.JsonLdProcessor.get_context_value(
                    active, index_key, "@language"
                )
                index_direction = self.runtime.JsonLdProcessor.get_context_value(
                    active, index_key, "@direction"
                )
                for item in (index_type, index_language, index_direction):
                    _text(item, self.budget, nullable=True)
        raw_scope = entry.get("@context", ABSENT)
        scope_json = (
            None
            if raw_scope is ABSENT
            else _dump(None if raw_scope is False else raw_scope, self.budget)
        )
        language = self.runtime.JsonLdProcessor.get_context_value(
            active, name, "@language"
        )
        direction = self.runtime.JsonLdProcessor.get_context_value(
            active, name, "@direction"
        )
        _text(language, self.budget, nullable=True)
        _text(direction, self.budget, nullable=True)
        nest = text("@nest")
        if nest is not ABSENT:
            nest = self.processor._expand_iri(active, nest, vocab=True)
            if nest != "@nest":
                raise _Unsupported("Unresolved nest keyword alias")
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
        self, active: Any, depth: int = 0, seen: frozenset[int] = frozenset()
    ) -> ContextView:
        """Capture only the qualified processor layout with bounded reversion state."""
        self.budget.consume("work")
        self.budget.consume("states")
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("Processed context reversion exceeds max_depth")
        if id(active) in seen:
            raise _Unsupported("Cyclic processed context reversion")
        state = _layout(active, _ACTIVE, self.budget)
        mode = _text(state.get("processingMode"), self.budget)
        base_mapping = _text(
            state.get("@base", ABSENT), self.budget, nullable=True, absent=True
        )
        try:
            effective_base = (
                self.processor._expand_iri(
                    active, "", base=self.base if self.base is not None else ""
                )
                if base_mapping is not ABSENT
                else self.base
            )
        except ValueError as error:
            raise _Unsupported("Native base resolution is not qualified") from error
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
            raise _Unsupported("Missing processed term inventory")
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
            self.view(previous, depth + 1, seen | {id(active)})
            if previous is not None
            else None
        )
        return ContextView(defaults, terms, prior)


def inspect_context(
    context: object,
    *,
    processing_mode: str = "json-ld-1.1",
    base: str | None = None,
    inherited_context: object | Absent = ABSENT,
    scopes: Sequence[ContextScope] = (),
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextInspection:
    """Inspect finite effective definitions, without proving document compatibility.

    Root processing starts from the initial context when inheritance is omitted.
    Selected scopes require an explicitly declared parent (None asserts initial).
    Unselected scoped combinations remain visible raw metadata, not instantiations.
    """
    runtime = processor()
    operation = _operation(
        limits,
        budget
        if budget is not None
        else document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None,
    )
    issues = IssueCollector(operation)
    root = None
    selected: tuple[ContextScope, ...] = ()
    scoped: list[tuple[ContextScope, ContextView | None]] = []
    checked = 0
    requested = 1
    complete = False
    data_json = '{"view":null,"scopes":[]}'
    try:
        bound_text(processing_mode, operation)
        if processing_mode not in ("json-ld-1.0", "json-ld-1.1"):
            raise InputError("Select JSON-LD 1.0 or 1.1 processing mode")
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
        selected = _capture(scopes, operation)
        requested += len(selected)
        seen = set()
        for item in selected:
            if not isinstance(item, ContextScope):
                raise InputError("Expected typed ContextScope selections")
            bound_text(item.term, operation)
            bound_text(item.kind, operation)
            if item.kind not in ("property", "type") or (item.term, item.kind) in seen:
                raise InputError("Context scope selections must be distinct and typed")
            seen.add((item.term, item.kind))
        local = _own(context, operation)
        inherited = (
            _own(inherited_context, operation)
            if inherited_context is not ABSENT
            else ABSENT
        )
        bridge = _Bridge(runtime, document_loader, base, processing_mode, operation)
        active = bridge.processor._get_initial_context(bridge.options)
        if inherited is not ABSENT:
            active = bridge.apply(active, inherited)
        active = bridge.apply(active, local)
        root = bridge.view(active)
        checked = 1
        for item in selected:
            operation.consume("work")
            if inherited is ABSENT:
                scoped.append((item, None))
                issues.append(
                    Issue(
                        "context.definition_unverified",
                        "Selected scope requires an explicit inherited parent",
                        kind="unverified",
                    )
                )
                continue
            definition = active["mappings"].get(item.term)
            if not isinstance(definition, Mapping) or "@context" not in definition:
                scoped.append((item, None))
                issues.append(
                    Issue(
                        "context.definition_unverified",
                        "Selected term has no established scoped context",
                        kind="unverified",
                    )
                )
                continue
            transitioned = bridge.apply(active, definition["@context"], scope=item)
            scoped.append((item, bridge.view(transitioned)))
            checked += 1
        data_json = _dump(
            {
                "view": _data(root, operation),
                "scopes": [
                    {
                        "selection": _data(item, operation),
                        "view": _data(view, operation),
                    }
                    for item, view in scoped
                ],
            },
            operation,
        )
        complete = checked == requested
    except (LimitExceeded, MemoryError, RecursionError):
        root = None
        scoped = []
        issues.exhausted("Processed context inspection exceeded its limits")
    except _Unsupported:
        root = None
        scoped = []
        try:
            issues.append(
                Issue(
                    "context.definition_unverified",
                    "Processor state or context scope is not qualified",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Context diagnostic allowance exhausted")
    except InputError as error:
        root = None
        scoped = []
        try:
            code = getattr(error, "processor_code", None)
            if isinstance(code, str):
                bound_text(code, operation)
            else:
                code = None
            retain_report(
                Report(
                    "context.definitions",
                    issues=(
                        Issue(
                            "context.input_error",
                            "Context processing rejected the supplied input",
                            kind="input_error",
                            actual=code,
                            details=Location.from_error(error),
                        ),
                    ),
                ),
                operation,
                issues,
            )
        except LimitExceeded:
            issues.exhausted("Context diagnostic allowance exhausted")
    return ContextInspection(
        root,
        tuple(scoped),
        Report(
            "context.definitions",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
        ),
        data_json,
    )
