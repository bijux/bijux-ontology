"""Maintained offline JSON Schema validation with bounded finite expansion."""

from collections.abc import Sequence
from typing import Any

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import measure_json

DIALECT = "https://json-schema.org/draft/2020-12/schema"
DRAFT07 = "http://json-schema.org/draft-07/schema#"
# These published BioSamples expressions have finite linear matching behavior.
# Arbitrary checklist regexes require a separately qualified execution boundary.
_DRAFT07_PATTERNS = frozenset(
    (
        "^.*$",
        "^DUO:[0-9]{7}$",
        "^SAM(D|N|E([AG]?))[0-9]+$",
        "(http|https)://www(dev)?.ebi.ac.uk/biosamples/schemas/structured/.*",
    )
)
_DRAFT07_KEYWORDS = frozenset(
    (
        "id",
        "definitions",
        "patternProperties",
        "pattern",
        "format",
        "examples",
    )
)
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


def backend(dialect: str = DIALECT) -> tuple[Any, Any, Any]:
    """Load maintained optional schema libraries only at the requested boundary."""
    try:
        from jsonschema import Draft7Validator, Draft202012Validator  # type: ignore[import-untyped]
        from referencing import Registry
        from referencing.jsonschema import DRAFT7, DRAFT202012
    except ImportError as error:
        raise ResourceError(
            "JSON Schema validation requires bijux-ontology[jsonschema]"
        ) from error
    if dialect == DRAFT07:
        return Draft7Validator, Registry, DRAFT7
    if dialect != DIALECT:
        raise InputError("Unsupported declared JSON Schema dialect")
    return Draft202012Validator, Registry, DRAFT202012


def _deny(uri: str) -> Any:
    """Never acquire a schema resource from the network or filesystem."""
    raise ResourceError("Generated schemas resolve only their owned local definitions")


def validator(
    schema: object,
    budget: Budget,
    *,
    dialect: str = DIALECT,
    root_iri: str | None = None,
    resources: Sequence[tuple[str, object]] = (),
) -> tuple[Any, int]:
    """Check maintained schema validity and bound expanded reference work first."""
    cls, registry_class, specification = backend(dialect)
    from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

    if not isinstance(schema, dict) or schema.get("$schema") != dialect:
        raise InputError("Expected the explicitly selected JSON Schema dialect")
    registry = registry_class(retrieve=_deny)
    iri = root_iri if root_iri is not None else schema.get("$id")
    if not isinstance(iri, str):
        raise InputError("Schemas require an explicit resource identifier")
    inventory = ((iri, schema), *capture(resources, budget))
    identities: set[str] = set()
    for uri, document in inventory:
        bound_text(uri, budget)
        budget.consume("resources")
        if uri in identities or not isinstance(document, dict):
            raise InputError("Schema resources need distinct named objects")
        identities.add(uri)
        if document.get("$schema") != dialect or document.get("$id", uri) != uri:
            raise InputError("Resources must retain the selected dialect and identity")
        registry = registry.with_resource(uri, specification.create_resource(document))
    resolver = registry.resolver(iri)
    allowed = (
        (_KEYWORDS - {"$defs"}) | _DRAFT07_KEYWORDS if dialect == DRAFT07 else _KEYWORDS
    )
    memo: dict[int, int] = {}

    def cost(node: object, current: Any, active: frozenset[int], depth: int) -> int:
        """Bound finite reference expansion before maintained schema evaluation."""
        budget.consume("work")
        budget.consume("states")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Generated schema references exceed max_depth")
        if type(node) is bool:
            return 1
        if not isinstance(node, dict) or set(node) - allowed:
            raise InputError(
                "Schema uses vocabulary outside the finite generation profile"
            )
        if dialect == DRAFT07:
            if "$schema" in node and node["$schema"] != dialect:
                raise InputError("Cross-dialect schema references are unsupported")
            if "pattern" in node and node["pattern"] not in _DRAFT07_PATTERNS:
                raise InputError("Regex is outside the qualified finite profile")
            if "format" in node and (
                node["format"] != "date-time"
                or "date-time" not in cls.FORMAT_CHECKER.checkers
            ):
                raise ResourceError("A maintained date-time format checker is required")
        identity = id(node)
        if identity in active:
            raise InputError("Recursive schemas have no finite validation-work bound")
        if identity in memo:
            return memo[identity]
        current = current.in_subresource(specification.create_resource(node))
        total = 1
        for key in ("enum", "const"):
            if key in node:
                total += measure_json(
                    node[key],
                    budget=budget,
                    max_bytes=budget.limits.max_bytes,
                    max_depth=budget.limits.max_depth,
                ).weight
        if "$ref" in node:
            resolved = current.lookup(node["$ref"])
            total += cost(
                resolved.contents, resolved.resolver, active | {identity}, depth + 1
            )
        children: list[object] = []
        for key in (
            ("properties", "definitions", "patternProperties")
            if dialect == DRAFT07
            else ("properties", "$defs")
        ):
            container = node.get(key, {})
            if not isinstance(container, dict):
                raise InputError("Schema child inventories must be objects")
            budget.consume("states", len(container))
            if key == "patternProperties" and set(container) - _DRAFT07_PATTERNS:
                raise InputError("Property regex is outside the finite profile")
            children.extend(container.values())
        if type(node.get("additionalProperties", True)) is not bool:
            if dialect != DRAFT07:
                raise InputError(
                    "Generated property closure must be an explicit boolean"
                )
            children.append(node["additionalProperties"])
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
    if dialect == DRAFT07:
        # Enter the resource through its registered identity. The published legacy
        # `id` is an annotation in draft-07, so it cannot establish a ref base.
        return cls(
            {"$schema": dialect, "$ref": iri},
            registry=registry,
            format_checker=cls.FORMAT_CHECKER,
        ), expanded
    return cls(schema, registry=registry), expanded
