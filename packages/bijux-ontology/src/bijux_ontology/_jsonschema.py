"""Maintained offline JSON Schema validation with bounded finite expansion."""

from collections import deque
from collections.abc import Sequence
from math import isfinite
from typing import Any

from bijux_ontology._json import own_json
from bijux_ontology._jsonpaths import bound_index
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits, bound_text

DIALECT = "https://json-schema.org/draft/2020-12/schema"
_KEYWORDS = frozenset(
    (
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "title",
        "description",
        "$comment",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "enum",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
    )
)


def backend() -> tuple[Any, Any, Any]:
    """Load maintained optional schema libraries only at the requested boundary."""
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
        from referencing import Registry
        from referencing.jsonschema import DRAFT202012
    except ImportError as error:
        raise ResourceError(
            "JSON Schema validation requires bijux-ontology[jsonschema]"
        ) from error
    return Draft202012Validator, Registry, DRAFT202012


def operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Keep one caller's limits and counters throughout each owned operation."""
    if budget is not None and (
        not isinstance(budget, Budget) or limits is not None and limits != budget.limits
    ):
        raise InputError("Use the shared Budget with matching limits")
    return budget if budget is not None else Budget(limits)


def capture(values: Sequence[Any], budget: Budget) -> tuple[Any, ...]:
    """Own a finite indexed inventory without trusting an arbitrary iterator."""
    if not isinstance(values, (Sequence, deque)) or isinstance(values, (str, bytes)):
        raise InputError("Output contracts require finite sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            result.append(values[index])
        except IndexError as error:
            raise InputError("Output inventory changed during capture") from error
    return tuple(result)


def _measure(value: object, budget: Budget) -> int:
    """Bound JSON nodes and scalar comparison payloads before owned processing."""
    count = 0

    def visit(item: object, depth: int) -> None:
        """Charge each actual JSON node before the maintained ownership copy."""
        nonlocal count
        budget.consume("work")
        budget.consume("states")
        count += 1
        if depth > budget.limits.max_depth:
            raise LimitExceeded("JSON output exceeds max_depth")
        if isinstance(item, str):
            bound_text(item, budget)
            count += len(item)
        elif item is None or type(item) is bool:
            return
        elif type(item) is int:
            bound_index(item, budget)
            count += max(1, (item.bit_length() * 30103) // 100000 + 1)
        elif type(item) is float:
            if not isfinite(item):
                raise InputError("JSON output numbers must be finite")
            bound_text(repr(item), budget)
            count += len(repr(item))
        elif type(item) is list:
            for child in capture(item, budget):
                visit(child, depth + 1)
        elif type(item) is dict:
            budget.consume("states", len(item))
            for key, child in item.items():
                if not isinstance(key, str):
                    raise InputError("JSON output object keys require text")
                bound_text(key, budget)
                count += len(key)
                visit(child, depth + 1)
        else:
            raise InputError("Expected finite JSON output values")

    visit(value, 0)
    return count


def owned_json(value: object, budget: Budget) -> tuple[object, int]:
    """Qualify finite JSON metadata before producing its independent snapshot."""
    count = _measure(value, budget)
    return own_json(
        value, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    ), count


def _deny(uri: str) -> Any:
    """Never acquire a schema resource from the network or filesystem."""
    raise ResourceError("Generated schemas resolve only their owned local definitions")


def validator(schema: object, budget: Budget) -> tuple[Any, int]:
    """Check maintained schema validity and bound expanded reference work first."""
    cls, registry_class, specification = backend()
    from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

    if not isinstance(schema, dict) or schema.get("$schema") != DIALECT:
        raise InputError("Expected the generated JSON Schema 2020-12 dialect")
    registry = registry_class(retrieve=_deny)
    iri = schema.get("$id")
    if not isinstance(iri, str):
        raise InputError("Generated schemas require an explicit resource identifier")
    registry = registry.with_resource(iri, specification.create_resource(schema))
    resolver = registry.resolver(iri)
    memo: dict[int, int] = {}

    def cost(node: object, current: Any, active: frozenset[int], depth: int) -> int:
        """Bound finite reference expansion before maintained schema evaluation."""
        budget.consume("work")
        budget.consume("states")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Generated schema references exceed max_depth")
        if type(node) is bool:
            return 1
        if not isinstance(node, dict) or set(node) - _KEYWORDS:
            raise InputError(
                "Schema uses vocabulary outside the finite generation profile"
            )
        identity = id(node)
        if identity in active:
            raise InputError("Recursive schemas have no finite validation-work bound")
        if identity in memo:
            return memo[identity]
        current = current.in_subresource(specification.create_resource(node))
        total = 1
        for key in ("enum", "const"):
            if key in node:
                total += _measure(node[key], budget)
        if "$ref" in node:
            resolved = current.lookup(node["$ref"])
            total += cost(
                resolved.contents, resolved.resolver, active | {identity}, depth + 1
            )
        children: list[object] = []
        for key in ("properties", "$defs"):
            container = node.get(key, {})
            if not isinstance(container, dict):
                raise InputError("Schema child inventories must be objects")
            budget.consume("states", len(container))
            children.extend(container.values())
        if type(node.get("additionalProperties", True)) is not bool:
            raise InputError("Generated property closure must be an explicit boolean")
        for key in ("items", "not", "if", "then", "else"):
            if key in node:
                budget.consume("states")
                children.append(node[key])
        for key in ("anyOf", "oneOf", "allOf"):
            container = node.get(key, ())
            budget.consume("states", len(container))
            children.extend(container)
        for child in children:
            total += cost(child, current, active | {identity}, depth + 1)
            if total > budget.limits.max_work:
                raise LimitExceeded("Expanded schema validation exceeds max_work")
        memo[identity] = total
        return total

    expanded = cost(schema, resolver, frozenset(), 0)
    budget.consume("work", expanded)
    try:
        cls.check_schema(schema)
    except SchemaError as error:
        raise InputError(
            "Generated schema is not valid in its declared dialect"
        ) from error
    return cls(schema, registry=registry), expanded
