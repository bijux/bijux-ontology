"""Finite offline JSON Schema contracts with separately authored RDF meaning."""

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urldefrag

from rdflib import Literal

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import FieldDeclaration, ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction
from bijux_ontology.interfaces.adapters.field_contracts import declare_field
from bijux_ontology.interfaces.adapters.jsonschema.types import SchemaModel
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri

DIALECT = "https://json-schema.org/draft/2020-12/schema"
_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$anchor",
        "$ref",
        "$defs",
        "$comment",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "enum",
        "const",
        "oneOf",
    }
)
_ANNOTATIONS = frozenset(
    {"$schema", "$id", "$anchor", "$defs", "$comment", "title", "description"}
)


def _backend() -> tuple[Any, Any, Any]:
    """Import maintained schema engines only at an explicitly requested boundary."""
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
        from referencing import Registry
        from referencing.jsonschema import DRAFT202012
    except ImportError as error:
        raise ResourceError("Install bijux-ontology[jsonschema]") from error
    return Draft202012Validator, Registry, DRAFT202012


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Reuse one explicit cumulative budget, rejecting conflicting limits."""
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Explicit limits conflict with the operation budget")
    return budget if budget is not None else Budget(limits)


def _charge(value: object, budget: Budget, depth: int = 0) -> int:
    """Bound JSON and typed sidecar metadata before copying or backend work."""
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Schema metadata exceeds max_depth")
    budget.consume("work")
    budget.consume("states")
    if isinstance(value, str):
        if len(value) > budget.limits.max_bytes:
            raise LimitExceeded("Schema text exceeds max_bytes")
        budget.consume("work", len(value))
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as error:
            raise InputError("Schema metadata must be Unicode text") from error
        size += 2 + sum(
            1 if char in '\\"\b\t\n\f\r' else 5 if ord(char) < 32 else 0
            for char in value
        )
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Schema text exceeds max_bytes")
        budget.consume("bytes", size)
        if isinstance(value, Literal):
            _charge(value.datatype, budget, depth + 1)
            _charge(value.language, budget, depth + 1)
        return 1 + len(value)
    if value is ABSENT:
        return 1
    if value is None or isinstance(value, bool):
        budget.consume("bytes", 4 if value is None or value is True else 5)
        return 1
    if isinstance(value, int):
        if value.bit_length() > budget.limits.max_bytes:
            raise LimitExceeded("Schema integer exceeds metadata bounds")
        budget.consume("work", value.bit_length() + 1)
        budget.consume("bytes", len(str(value)))
        return 1 + value.bit_length()
    if isinstance(value, (float, Decimal)):
        return _charge(str(value), budget, depth + 1)
    count = 1
    if isinstance(value, Mapping):
        budget.consume("bytes", 2 + max(0, len(value) - 1) + len(value))
        for key, child in value.items():
            count += _charge(key, budget, depth + 1)
            count += _charge(child, budget, depth + 1)
    elif isinstance(value, (list, tuple)):
        budget.consume("bytes", 2 + max(0, len(value) - 1))
        for index in range(len(value)):
            count += _charge(value[index], budget, depth + 1)
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            count += _charge(getattr(value, field.name), budget, depth + 1)
    else:
        raise InputError("Unsupported schema or mapping metadata")
    return count


def _owned(value: object, budget: Budget) -> object:
    """Capture JSON only after its original metadata has been bounded."""
    _charge(value, budget)
    return own_json(
        value, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    )


def _deny(uri: str) -> Any:
    """Never interpret a missing schema identifier as a retrieval instruction."""
    from referencing.exceptions import NoSuchResource

    raise NoSuchResource(uri)


class _Schema:
    """One operation's owned schema graph, mappings and finite translation scope."""

    def __init__(
        self,
        schema: object,
        mappings: Sequence[SchemaModel],
        resources: Mapping[str, object],
        budget: Budget,
        issues: IssueCollector | None = None,
    ) -> None:
        """Capture offline resources and qualify supported schema graph structure."""
        validator, registry, specification = _backend()
        self.budget = budget
        self.issues = issues if issues is not None else IssueCollector(budget)
        self.checked = 0
        self.safe = True
        self.schema = _owned(schema, budget)
        if not isinstance(self.schema, dict) or self.schema.get("$schema") != DIALECT:
            raise InputError("Declare the JSON Schema 2020-12 dialect explicitly")
        uri = self.schema.get("$id")
        if not isinstance(uri, str):
            raise InputError("The root schema requires an explicit absolute $id")
        absolute_iri(uri)
        if urldefrag(uri).fragment:
            raise InputError("Schema resource $id must not contain a nonempty fragment")
        if not isinstance(resources, Mapping):
            raise InputError("Schema references require supplied offline resources")
        self.registry = registry(retrieve=_deny).with_resource(
            uri, specification.create_resource(self.schema)
        )
        if len(resources) > budget.limits.max_resources:
            raise LimitExceeded("Schema references exceed max_resources")
        self.roots = {id(self.schema)}
        for key, resource in resources.items():
            _charge(key, budget)
            absolute_iri(key)
            budget.consume("resources")
            owned = _owned(resource, budget)
            if not isinstance(owned, dict) or owned.get("$schema", DIALECT) != DIALECT:
                raise InputError("Referenced resources must use JSON Schema 2020-12")
            if owned.get("$id", key) != key:
                raise InputError(
                    "Supplied resource identifier conflicts with its explicit $id"
                )
            if key == uri and owned != self.schema:
                raise InputError("Supplied resource conflicts with the root schema")
            self.registry = self.registry.with_resource(
                key, specification.create_resource(owned)
            )
            self.roots.add(id(owned))
        self.resolver = self.registry.resolver(uri)
        self.specification = specification
        self.validator = validator(self.schema, registry=self.registry)
        self.validator_class = validator
        if isinstance(cast(object, mappings), (str, bytes)) or not isinstance(
            mappings, Sequence
        ):
            raise InputError("Schema mappings require a finite sequence")
        count = len(mappings)
        if count > budget.limits.max_states:
            raise LimitExceeded("Schema mappings exceed max_states")
        captured = []
        for index in range(count):
            item = mappings[index]
            if not isinstance(item, SchemaModel):
                raise InputError("Expected explicit SchemaModel mappings")
            _charge(item, budget)
            captured.append(item)
        self.mappings = tuple(captured)
        self.models: dict[int, SchemaModel] = {}
        names: set[str] = set()
        for item in self.mappings:
            if item.identifier in names:
                raise InputError("Schema model identifiers must be unique")
            names.add(item.identifier)
            try:
                resolved = self.resolver.lookup(item.schema_uri)
            except (MemoryError, RecursionError, LimitExceeded):
                raise
            except Exception:
                self.unknown("Mapping references an unavailable supplied schema")
                continue
            if id(resolved.contents) in self.models:
                raise InputError("Multiple RDF mappings select the same schema object")
            self.models[id(resolved.contents)] = item
        self.objects: dict[int, dict[str, Any]] = {}
        self.costs: dict[int, int] = {}
        self.validation_cost = self.visit(self.schema, self.resolver, set(), 0)
        if self.safe:
            validator.check_schema(self.schema)

    def unknown(self, message: str, *, safe: bool = True) -> None:
        """Retain required unsupported scope without inventing a validation result."""
        self.safe = self.safe and safe
        self.issues.append(
            Issue("adapter.schema_unverified", message, kind="unverified")
        )

    def resolve(self, node: Any, resolver: Any) -> tuple[Any, Any]:
        """Use the maintained reference resolver, never a custom pointer parser."""
        if isinstance(node, dict):
            resolver = resolver.in_subresource(self.specification.create_resource(node))
            if "$ref" in node:
                resolved = resolver.lookup(node["$ref"])
                return resolved.contents, resolved.resolver
        return node, resolver

    def visit(self, node: Any, resolver: Any, active: set[int], depth: int) -> int:
        """Qualify a finite acyclic schema graph with memoized repeated references."""
        self.budget.consume("work")
        self.budget.consume("states")
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("Schema references exceed max_depth")
        if not isinstance(node, dict):
            self.unknown(
                "Boolean subschemas are outside the RDF translation profile", safe=False
            )
            return 1
        identity = id(node)
        if identity in active:
            self.unknown(
                "Recursive schema references need an explicit unsupported scope",
                safe=False,
            )
            return 1
        if identity in self.costs:
            return self.costs[identity]
        active = active | {identity}
        self.checked += 1
        if set(node) - _KEYWORDS or node.get("$schema", DIALECT) != DIALECT:
            self.unknown("Schema uses an unsupported keyword or dialect", safe=False)
        if "$anchor" in node or "$id" in node and identity not in self.roots:
            self.unknown(
                "Embedded resource identifiers and anchors are outside this profile",
                safe=False,
            )
        if "$ref" in node and set(node) - _ANNOTATIONS - {"$ref"}:
            self.unknown(
                "Adjacent reference constraints are not translated", safe=False
            )
        if "oneOf" in node and set(node) - _ANNOTATIONS - {"oneOf", "type"}:
            self.unknown(
                "Discriminated alternatives cannot have adjacent field constraints",
                safe=False,
            )
        try:
            resolved, selected = self.resolve(node, resolver)
        except (MemoryError, RecursionError, LimitExceeded):
            raise
        except Exception:
            self.unknown("A schema reference is not available offline", safe=False)
            return 1
        if resolved is not node:
            cost = 1 + self.visit(resolved, selected, active, depth + 1)
        else:
            cost = 1
            for keyword, literal in node.items():
                self.budget.consume("work")
                if keyword not in {"$defs", "properties", "items", "oneOf"}:
                    cost += _charge(literal, self.budget)
            if "oneOf" in node:
                branches = node["oneOf"]
                if not isinstance(branches, list) or not branches:
                    raise InputError("oneOf requires finite schema alternatives")
                if self.discriminator(branches, selected) is None:
                    self.unknown(
                        "oneOf requires a shared required scalar const discriminator",
                        safe=False,
                    )
                for child in branches:
                    cost += self.visit(child, selected, active, depth + 1)
            else:
                properties = node.get("properties", {})
                if not isinstance(properties, dict):
                    raise InputError("Schema properties require named subschemas")
                if node.get("type") == "object" or properties:
                    self.objects[identity] = node
                    if identity not in self.models:
                        self.unknown("Object schema has no explicit RDF model mapping")
                for key, child in properties.items():
                    cost += _charge(key, self.budget)
                    cost += self.visit(child, selected, active, depth + 1)
                if "items" in node:
                    cost += self.visit(node["items"], selected, active, depth + 1)
                if isinstance(node.get("additionalProperties"), dict):
                    self.unknown(
                        "Schema-valued additionalProperties is not translated",
                        safe=False,
                    )
            for child in node.get("$defs", {}).values():
                cost += self.visit(child, selected, active, depth + 1)
        if cost > self.budget.limits.max_work:
            raise LimitExceeded("Expanded schema validation cost exceeds max_work")
        self.costs[identity] = cost
        return cost

    def discriminator(self, branches: list[Any], resolver: Any) -> str | None:
        """Qualify only explicit distinct scalar tags, not guessed union branches."""
        candidates: dict[str, list[object]] | None = None
        for branch in branches:
            self.budget.consume("work")
            try:
                node, _ = self.resolve(branch, resolver)
            except (MemoryError, RecursionError, LimitExceeded):
                raise
            except Exception:
                return None
            if not isinstance(node, dict):
                return None
            properties = node.get("properties", {})
            required = node.get("required", [])
            if not isinstance(properties, dict) or not isinstance(required, list):
                return None
            self.budget.consume(
                "work",
                sum((len(key) + 1) * (len(required) + 1) for key in properties),
            )
            values = {
                key: value["const"]
                for key, value in properties.items()
                if key in required
                and isinstance(value, dict)
                and "const" in value
                and (value["const"] is None or type(value["const"]) in (str, int, bool))
            }
            if candidates is None:
                candidates = {key: [value] for key, value in values.items()}
            else:
                for key, seen in candidates.items():
                    value = values.get(key)
                    width = (
                        len(value)
                        if isinstance(value, str)
                        else value.bit_length()
                        if isinstance(value, int)
                        else 1
                    )
                    self.budget.consume(
                        "work", (width + len(key) + 1) * (len(seen) + 1)
                    )
                candidates = {
                    key: [*seen, values[key]]
                    for key, seen in candidates.items()
                    if key in values
                    and not any(
                        type(old) is type(values[key]) and old == values[key]
                        for old in seen
                    )
                }
        return next(iter(candidates), None) if candidates else None

    def extraction(self) -> ModelExtraction:
        """Translate each mapped object independently; alternatives stay separate."""
        declarations = []
        identifiers = frozenset(item.identifier for item in self.mappings)
        for identity, node in self.objects.items():
            self.budget.consume("work")
            mapping = self.models.get(identity)
            if mapping is None:
                continue
            properties = node.get("properties", {})
            supplied = dict(mapping.fields)
            requirements = dict(mapping.requirements)
            if set(supplied) - set(properties):
                raise InputError("RDF mapping names a property absent from its schema")
            declared = []
            for key in properties:
                self.budget.consume("work")
                if key in {"@context", "@id", "@type"} and key not in supplied:
                    continue
                field = declare_field(
                    key,
                    key,
                    supplied.get(key),
                    identifiers=identifiers,
                    location=mapping.identifier,
                    issues=self.issues,
                )
                if field is not None:
                    declared.append(
                        FieldDeclaration(
                            field.name,
                            field.predicate,
                            field.values,
                            key=field.key,
                            reverse=field.reverse,
                            emission=field.emission,
                            output=requirements.get(key),
                        )
                    )
            declarations.append(
                ModelDeclaration(mapping.identifier, mapping.types, tuple(declared))
            )
        return ModelExtraction(tuple(declarations), self.report())

    def report(self) -> Report:
        """Describe finite RDF extraction scope without universal serializer claims."""
        return Report(
            "jsonschema.extraction",
            checked=self.checked,
            requested=max(1, self.checked),
            issues=tuple(self.issues),
            families=("explicit-serialization", "json-schema-2020-12"),
        )


def model_declarations(
    schema: object,
    mappings: Sequence[SchemaModel],
    *,
    resources: Mapping[str, object] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ModelExtraction:
    """Import finite explicit RDF sidecars; input required does not imply emission."""
    operation = _operation(limits, budget)
    _backend()
    issues = IssueCollector(operation)
    try:
        return _Schema(
            schema, mappings, resources or {}, operation, issues
        ).extraction()
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("JSON Schema extraction exceeded its limits")
    except Exception:
        try:
            issues.append(
                Issue(
                    "input.invalid",
                    "Schema or serializer input could not be processed",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("JSON Schema diagnostics exceeded their limit")
    return ModelExtraction(
        (),
        Report(
            "jsonschema.extraction", requested=1, complete=False, issues=tuple(issues)
        ),
    )
