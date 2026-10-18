"""Compile explicit native JSON output intent without equating JSON and RDF."""

import json
from collections.abc import Sequence
from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from sys import getsizeof
from typing import Literal as Status
from urllib.parse import quote, urldefrag

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.jsonschema.types import SchemaModel
from bijux_ontology.jsonld.paths import bound_index, bound_pointer_text
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.terms import bound_node, bound_text, literal_term
from bijux_ontology.reports import Issue, Report

from .types import CompiledOutputSchema, JsonRepresentation, SchemaTranslation
from .validation import DIALECT, backend, capture, operation, owned_json, validator


def _contract(item: object, budget: Budget, depth: int = 0) -> None:
    """Bound authored immutable metadata before retaining or formatting intent."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Output contract metadata exceeds max_depth")
    if isinstance(item, URIRef | BNode | Literal):
        bound_node(item, budget)
    elif isinstance(item, str):
        bound_text(item, budget)
    elif item is None or item is ABSENT or type(item) is bool:
        return
    elif type(item) is int:
        bound_index(item, budget)
    elif isinstance(item, Decimal):
        size = getsizeof(item)
        digits = abs(item.adjusted()) + 1
        if digits > budget.limits.max_bytes:
            raise LimitExceeded("Numeric output bounds exceed max_bytes")
        budget.consume("work", size + digits)
        budget.consume("bytes", size + 3 * digits)
    elif isinstance(item, tuple | list):
        for child in capture(item, budget):
            _contract(child, budget, depth + 1)
    elif is_dataclass(item) and not isinstance(item, type):
        for field in fields(item):
            _contract(getattr(item, field.name), budget, depth + 1)
    else:
        raise InputError("Output declarations require finite supported metadata")


def _native(term: URIRef | Literal, budget: Budget) -> object:
    """Convert only exact canonical native values, never RDF value-space equality."""
    if isinstance(term, URIRef):
        return str(term)
    if not check_literal(term, budget=budget).verified or term.language is not None:
        raise InputError("Enumeration lacks exact native JSON encoding")
    if term.datatype in (None, XSD.string):
        return str(term)
    if term.datatype == XSD.boolean and str(term) in ("true", "false"):
        return str(term) == "true"
    if term.datatype == XSD.integer:
        value = term.toPython()
        if type(value) is int and str(value) == str(term):
            return value
    raise InputError("Enumeration lacks exact native JSON encoding")


def _literal(term: Literal, budget: Budget) -> Literal:
    """Own original lexical metadata before returning a reusable RDF mapping."""
    bound_node(term, budget)
    return literal_term(
        str(term),
        datatype=str(term.datatype) if term.datatype is not None else None,
        language=term.language,
    )


def _values(
    values: tuple[ValueDeclaration, ...], budget: Budget
) -> tuple[ValueDeclaration, ...]:
    """Snapshot mutable RDF literal metadata inside otherwise frozen declarations."""
    budget.consume("states", len(values))
    result = []
    for value in values:
        budget.consume("work")
        samples = tuple(
            _literal(term, budget) for term in capture(value.samples, budget)
        )
        constraints = value.constraints
        if constraints is not None and constraints.enumeration is not None:
            enumeration = constraints.enumeration
            terms = (
                enumeration.values
                if isinstance(enumeration, ValueEnumeration)
                else enumeration
            )
            selected = tuple(
                _literal(term, budget) if isinstance(term, Literal) else term
                for term in capture(terms, budget)
            )
            if isinstance(enumeration, ValueEnumeration):
                excluded = tuple(
                    _literal(term, budget) if isinstance(term, Literal) else term
                    for term in capture(enumeration.excluded, budget)
                )
                enumeration = replace(enumeration, values=selected, excluded=excluded)
            else:
                enumeration = selected
            constraints = replace(constraints, enumeration=enumeration)
        result.append(replace(value, samples=samples, constraints=constraints))
    return tuple(result)


class _Compiler:
    """One finite selected contract, with no serializer introspection or inference."""

    def __init__(self, budget: Budget) -> None:
        """Retain only bounded declarations, local definitions and translation facts."""
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.items: list[SchemaTranslation] = []
        self.children: list[Report] = []
        self.retained: set[int] = set()
        self.models: dict[str, ModelDeclaration] = {}
        self.representations: dict[tuple[str, str], str] = {}
        self.definitions: dict[str, object] = {}
        self.annotations: dict[str, object] = {}
        self.mappings: list[SchemaModel] = []
        self.labels: dict[str, str] = {}
        self.iri = ""
        self.profile = ""

    def record(
        self,
        model: str,
        field: str | None,
        obligation: str,
        detail: str,
        *,
        supported: bool = True,
        status: Status["preserved", "strengthened", "weakened", "unsupported"]
        | None = None,
    ) -> None:
        """Retain each explicit obligation under the same operation's quotas."""
        self.budget.consume("states")
        for text in (model, field or "", obligation, detail):
            bound_text(text, self.budget)
        selected_status = status or ("preserved" if supported else "unsupported")
        self.items.append(
            SchemaTranslation(
                model,
                field,
                obligation,
                selected_status,
                detail,
            )
        )
        if selected_status != "preserved":
            self.issues.append(
                Issue("schema.translation_unverified", detail, model, kind="unverified")
            )

    def reference(self, identifier: str) -> str:
        """Use a bounded JSON Pointer component in the owned schema resource."""
        size = bound_pointer_text(identifier, self.budget)
        if 3 * size + 10 > self.budget.limits.max_bytes:
            raise LimitExceeded("Schema reference exceeds max_bytes")
        self.budget.consume("bytes", 3 * size + 10)
        return "#/$defs/" + quote(
            identifier.replace("~", "~0").replace("/", "~1"), safe=""
        )

    def constraints(
        self, schema: dict[str, object], value: ValueDeclaration, model: str, name: str
    ) -> object:
        """Preserve native scalar predicates only where JSON comparison is faithful."""
        selected = value.constraints
        if selected is None:
            return schema
        for key, bound in (
            ("minimum", selected.minimum),
            ("maximum", selected.maximum),
        ):
            if bound is None:
                continue
            if schema.get("type") == "integer" and bound == bound.to_integral_value():
                number = int(bound)
                bound_index(number, self.budget)
                inclusive = (
                    selected.minimum_inclusive
                    if key == "minimum"
                    else selected.maximum_inclusive
                )
                keyword = key if inclusive else "exclusive" + key.title()
                schema[keyword] = number
                self.record(
                    model, name, key, "Integral native JSON numeric bound is preserved"
                )
            else:
                self.record(
                    model,
                    name,
                    key,
                    "Numeric conversion has no qualified exact JSON encoding",
                    supported=False,
                )
        for key, count in (
            ("minLength", selected.min_length),
            ("maxLength", selected.max_length),
        ):
            if count is None:
                continue
            if schema.get("type") == "string" and value.kind == "literal":
                schema[key] = count
                self.record(
                    model, name, key, "Original Unicode string length is preserved"
                )
            else:
                self.record(
                    model,
                    name,
                    key,
                    "String-length semantics are unavailable for this encoding",
                    supported=False,
                )
        if selected.pattern is not None:
            self.record(
                model,
                name,
                "pattern",
                "RE2 patterns are not silently translated to JSON Schema regex syntax",
                supported=False,
            )
        if selected.iri_namespaces:
            self.record(
                model,
                name,
                "IRI namespace",
                "IRI resolution and namespace membership remain RDF obligations",
                supported=False,
            )
        enumeration = selected.enumeration
        if enumeration is not None:
            if isinstance(enumeration, ValueEnumeration):
                if (
                    enumeration.report is not None
                    and id(enumeration.report) not in self.retained
                ):
                    retain_report(enumeration.report, self.budget, self.issues)
                    self.children.append(enumeration.report)
                    self.retained.add(id(enumeration.report))
                if not enumeration.qualified:
                    self.record(
                        model,
                        name,
                        "enumeration",
                        "Open or incomplete RDF enumeration cannot exclude JSON values",
                        supported=False,
                    )
                    return schema
                terms = enumeration.values
            else:
                terms = enumeration
            try:
                values = [_native(term, self.budget) for term in terms]
            except LimitExceeded:
                raise
            except InputError:
                self.record(
                    model,
                    name,
                    "enumeration",
                    "RDF lexical identity has no exact native JSON enumeration",
                    supported=False,
                )
            else:
                self.record(
                    model,
                    name,
                    "enumeration",
                    "Exact native scalar enumeration is preserved",
                )
                if not values:
                    return False
                schema["const" if len(values) == 1 else "enum"] = (
                    values[0] if len(values) == 1 else values
                )
        return schema

    def branch(
        self, value: ValueDeclaration, model: str, field: str, active: frozenset[str]
    ) -> object:
        """Translate one declared alternative without dropping unsupported branches."""
        self.budget.consume("states", 2)
        if value.kind == "literal":
            native = {
                str(XSD.string): "string",
                str(XSD.boolean): "boolean",
                str(XSD.integer): "integer",
            }
            if value.datatype is None:
                result: dict[str, object] = {"type": ["string", "boolean", "number"]}
            elif value.datatype in native:
                result = {"type": native[value.datatype]}
            else:
                self.record(
                    model,
                    field,
                    "datatype",
                    "Datatype has no supported native JSON representation",
                    supported=False,
                )
                return {}
            if value.datatype == str(XSD.integer):
                self.record(
                    model,
                    field,
                    "RDF numeric identity",
                    "JSON integer equality does not prove exact RDF identity",
                    status="weakened",
                )
            if value.language not in (None, ABSENT) or value.direction not in (
                None,
                ABSENT,
            ):
                self.record(
                    model,
                    field,
                    "literal metadata",
                    "Language, direction and context coercion remain RDF obligations",
                    supported=False,
                )
            self.record(
                model,
                field,
                "value branch",
                "Explicit native literal representation is preserved",
            )
        elif value.kind in ("iri", "object"):
            if value.model is not None and value.kind == "object":
                if value.model in active or value.model not in self.models:
                    self.record(
                        model,
                        field,
                        "nested model",
                        "Recursive or absent output models remain untranslated",
                        supported=False,
                    )
                    result = {"type": "object"}
                else:
                    self.model(value.model, active)
                    result = {"$ref": self.reference(value.model)}
            else:
                result = {"type": "string" if value.kind == "iri" else "object"}
            self.record(
                model,
                field,
                "RDF resource",
                "Resource identity, typing and context remain RDF obligations",
                supported=False,
            )
        else:
            self.record(
                model,
                field,
                "value branch",
                "Unknown RDF kind cannot constrain the serialized JSON branch",
                supported=False,
            )
            return {}
        if value.types or value.role is not None:
            self.record(
                model,
                field,
                "resource typing",
                "RDF class and role obligations have no JSON-only proof",
                supported=False,
            )
        if value.samples:
            self.record(
                model,
                field,
                "authored literal samples",
                "Authored RDF samples are not JSON value enumerations",
                supported=False,
            )
        return self.constraints(result, value, model, field)

    def field(
        self, model: str, field: FieldDeclaration, active: frozenset[str]
    ) -> object:
        """Keep source presence, native value shape and RDF counts separate."""
        if self.profile != "native-jsonld":
            self.record(
                model,
                field.name,
                "serializer representation",
                "Custom output encoding has no translated native value constraints",
                supported=False,
            )
            return {}
        shape = self.representations.get((model, field.name), "scalar")
        listed = all(value.container == "@list" for value in field.values)
        if listed != (shape == "list") or any(
            value.container not in (None, "@set", "@list") for value in field.values
        ):
            self.record(
                model,
                field.name,
                "container",
                "Selected JSON shape does not qualify the declared RDF container",
                supported=False,
            )
            return {}
        branches = [
            self.branch(value, model, field.name, active) for value in field.values
        ]
        schema: object = branches[0] if len(branches) == 1 else {"anyOf": branches}
        requirement = field.output
        if shape in ("array", "list"):
            self.record(
                model,
                field.name,
                "native collection members",
                "Selected native member forms exclude null and dropped representations",
                status="strengthened",
            )
            members: dict[str, object] = {"type": "array", "items": schema}
            if shape == "array" and (
                field.emission == "guaranteed"
                or requirement is not None
                and not requirement.allow_empty
            ):
                members["minItems"] = 1
            if shape == "list":
                faithful = all(
                    value.kind == "literal"
                    and value.datatype
                    in (None, str(XSD.string), str(XSD.boolean), str(XSD.integer))
                    for value in field.values
                )
                if requirement is not None:
                    for key, count in (
                        ("minItems", requirement.minimum_list_members),
                        ("maxItems", requirement.maximum_list_members),
                    ):
                        if count is not None and faithful:
                            members[key] = count
                            self.record(
                                model,
                                field.name,
                                "list member positions",
                                "Nonnull list positions preserve the declared count",
                            )
                        elif count is not None:
                            self.record(
                                model,
                                field.name,
                                "list member positions",
                                "Ambiguous RDF members have no qualified JSON count",
                                supported=False,
                            )
                schema = {
                    "type": "object",
                    "properties": {"@list": members},
                    "required": ["@list"],
                    "additionalProperties": False,
                }
            else:
                schema = members
        if requirement is not None:
            if shape != "list" and (
                requirement.minimum_list_members is not None
                or requirement.maximum_list_members is not None
            ):
                self.record(
                    model,
                    field.name,
                    "list member positions",
                    "List-member requirements need an explicit list representation",
                    supported=False,
                )
            if (
                requirement.minimum_values is not None
                or requirement.maximum_values is not None
            ):
                if shape == "scalar" and all(
                    value.kind == "literal"
                    and value.datatype
                    in (None, str(XSD.string), str(XSD.boolean), str(XSD.integer))
                    for value in field.values
                ):
                    if (
                        (requirement.minimum_values or 0) > 1
                        or requirement.maximum_values is not None
                        and requirement.maximum_values < 1
                    ):
                        schema = False
                    self.record(
                        model,
                        field.name,
                        "distinct RDF values",
                        "Nonnull native scalar encoding contributes one RDF value",
                    )
                else:
                    self.record(
                        model,
                        field.name,
                        "distinct RDF values",
                        "RDF distinct-value cardinality is not JSON array length",
                        supported=False,
                    )
            nullable = (
                requirement.allow_null
                and requirement.allow_empty
                and field.emission != "guaranteed"
                and not (requirement.minimum_values or 0)
                and requirement.minimum_list_members is None
                and requirement.maximum_list_members is None
            )
            if nullable:
                schema = {"anyOf": [schema, {"type": "null"}]}
            self.record(
                model,
                field.name,
                "null/empty output",
                "Explicit null/empty source permissions are retained",
            )
        else:
            self.record(
                model,
                field.name,
                "null/empty output",
                "No explicit null/empty output contract was supplied",
                supported=False,
            )
        return schema

    def discriminated(self, roots: tuple[str, ...]) -> bool:
        """Use oneOf only for mutually exclusive required scalar string constants."""
        first = self.definitions[roots[0]]
        if not isinstance(first, dict):
            return False
        for key in first.get("required", ()):
            self.budget.consume("work")
            tags: set[str] = set()
            for name in roots:
                self.budget.consume("work")
                definition = self.definitions[name]
                if not isinstance(definition, dict) or key not in definition.get(
                    "required", ()
                ):
                    break
                branch = definition["properties"].get(key)
                if not isinstance(branch, dict) or type(branch.get("const")) is not str:
                    break
                tag = branch["const"]
                if tag in tags:
                    break
                self.budget.consume("states")
                tags.add(tag)
            else:
                return True
        return False

    def model(self, identifier: str, active: frozenset[str]) -> None:
        """Emit one local definition and its separately linked RDF mapping."""
        if identifier in self.definitions:
            return
        if len(active) >= self.budget.limits.max_depth:
            raise LimitExceeded("Nested output models exceed max_depth")
        selected = self.models[identifier]
        properties: dict[str, object] = {}
        required = []
        mappings = []
        requirements = []
        field_annotations: dict[str, object] = {}
        annotations: dict[str, object] = {
            "types": list(selected.types),
            "fields": field_annotations,
        }
        self.record(
            identifier,
            None,
            "output object",
            "Explicit model object output is retained",
        )
        for field in selected.fields:
            self.budget.consume("states", 4)
            key = field.key or field.name
            if key in properties:
                raise InputError("Output model contains conflicting serialized keys")
            properties[key] = self.field(identifier, field, active | {identifier})
            if (
                field.emission == "guaranteed"
                or field.output is not None
                and (
                    field.output.require_property
                    or (field.output.minimum_values or 0) > 0
                    or field.output.minimum_list_members is not None
                    or field.output.maximum_list_members is not None
                )
            ):
                required.append(key)
                self.record(
                    identifier,
                    field.name,
                    "required source key",
                    "Authored output presence is preserved",
                )
            mappings.append(
                (
                    key,
                    RdfField(
                        field.predicate,
                        _values(field.values, self.budget),
                        reverse=field.reverse,
                        emission=field.emission,
                        output_contract=self.profile == "native-jsonld",
                    ),
                )
            )
            if field.output is not None:
                requirements.append((key, field.output))
            field_annotations[key] = {
                "predicate": field.predicate,
                "reverse": field.reverse,
                "label": self.labels.get(field.predicate),
                "values": [
                    {
                        "kind": value.kind,
                        "datatype": value.datatype,
                        "types": list(value.types),
                        "model": value.model,
                        "container": value.container,
                        "language": {"absent": True}
                        if value.language is ABSENT
                        else value.language,
                        "direction": {"absent": True}
                        if value.direction is ABSENT
                        else value.direction,
                    }
                    for value in field.values
                ],
            }
        result: dict[str, object] = {"type": "object", "properties": properties}
        if required:
            result["required"] = required
        self.budget.consume("states", 3)
        self.definitions[identifier] = result
        self.annotations[identifier] = annotations
        self.mappings.append(
            SchemaModel(
                self.iri + self.reference(identifier),
                identifier,
                selected.types,
                tuple(mappings),
                tuple(requirements),
            )
        )
        self.record(
            identifier,
            None,
            "context interpretation",
            "JSON conformance cannot establish RDF subjects or context interpretation",
            supported=False,
        )
        if (
            selected.types
            or selected.subject.expected_iri is not None
            or not selected.subject.allow_blank
        ):
            self.record(
                identifier,
                None,
                "subject identity/types",
                "RDF subject identity and asserted types remain linked annotations",
                supported=False,
            )
        if selected.coverage is not None or selected.emission_groups:
            self.record(
                identifier,
                None,
                "scope/scenarios",
                "RDF coverage and emission scenarios remain untranslated",
                supported=False,
            )


def compile_output_schema(
    models: Sequence[ModelDeclaration],
    roots: Sequence[str],
    *,
    schema_iri: str,
    representations: Sequence[JsonRepresentation] = (),
    profile: str = "native-jsonld",
    labels: Sequence[tuple[str, str]] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CompiledOutputSchema:
    """Compile an explicitly selected JSON representation with qualified losses.

    The profile is authored intent, not inferred universal serializer behavior.
    General anyOf schemas remain usable but are outside the import adapter's
    finite discriminated-oneOf subset. Linked mappings keep RDF meaning explicit.
    """
    backend()
    selected = operation(limits, budget)
    compiler = _Compiler(selected)
    schema_json = None
    annotations_json = "{}"
    complete = False
    try:
        if not isinstance(schema_iri, str) or isinstance(schema_iri, BNode | Literal):
            raise InputError("Schema resource identifier requires a named IRI")
        bound_text(schema_iri, selected)
        absolute_iri(schema_iri)
        if urldefrag(schema_iri).fragment:
            raise InputError(
                "Generated schema resource identifiers cannot have fragments"
            )
        bound_text(profile, selected)
        compiler.iri, compiler.profile = schema_iri, profile
        for model in capture(models, selected):
            if not isinstance(model, ModelDeclaration):
                raise InputError("Expected explicit ModelDeclaration output contracts")
            _contract(model, selected)
            if model.identifier in compiler.models:
                raise InputError("Generated model identifiers must be unique")
            compiler.models[model.identifier] = model
        requested = capture(roots, selected)
        if not requested:
            raise InputError("Select at least one output root model")
        for name in requested:
            bound_text(name, selected)
            if name not in compiler.models:
                raise InputError("Selected root model is not supplied")
        for item in capture(representations, selected):
            if not isinstance(item, JsonRepresentation):
                raise InputError("Expected an explicit JSON representation")
            _contract(item, selected)
            key = (item.model, item.field)
            if item.model in compiler.models:
                selected.consume("work", len(compiler.models[item.model].fields))
            if (
                item.model not in compiler.models
                or item.field
                not in {field.name for field in compiler.models[item.model].fields}
                or key in compiler.representations
            ):
                raise InputError(
                    "Representation selects an absent or duplicate output field"
                )
            compiler.representations[key] = item.shape
        for pair in capture(labels, selected):
            captured = capture(pair, selected)
            if len(captured) != 2:
                raise InputError("Ontology labels require IRI and text pairs")
            iri, label = captured
            if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
                raise InputError("Ontology labels require named IRIs")
            bound_text(iri, selected)
            absolute_iri(iri)
            bound_text(label, selected)
            if iri in compiler.labels:
                raise InputError("Ontology annotation labels must be unique")
            compiler.labels[iri] = label
        if profile != "native-jsonld":
            compiler.record(
                "output",
                None,
                "serializer profile",
                "A custom serializer has no qualified universal native output profile",
                supported=False,
            )
        for name in requested:
            compiler.model(name, frozenset())
        refs = [{"$ref": compiler.reference(name)} for name in requested]
        schema: dict[str, object] = {
            "$schema": DIALECT,
            "$id": schema_iri,
            "$defs": compiler.definitions,
        }
        if len(refs) == 1:
            schema.update(refs[0])
        else:
            schema["oneOf" if compiler.discriminated(requested) else "anyOf"] = refs
        owned, _ = owned_json(schema, selected)
        validator(owned, selected)
        schema_json = json.dumps(
            owned, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        selected.consume("bytes", len(schema_json.encode("utf-8")))
        annotations, _ = owned_json(
            {
                "schema": schema_iri,
                "models": compiler.annotations,
                "labels": compiler.labels,
            },
            selected,
        )
        annotations_json = json.dumps(
            annotations, ensure_ascii=False, separators=(",", ":")
        )
        complete = True
    except (LimitExceeded, MemoryError, RecursionError):
        schema_json = None
        compiler.issues.exhausted("JSON output schema compilation exceeded its limits")
    except (InputError, ValueError, TypeError):
        schema_json = None
        try:
            compiler.issues.append(
                Issue(
                    "schema.translation_invalid",
                    "Output contracts or representations could not be compiled",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            compiler.issues.exhausted(
                "JSON output schema diagnostics exceeded their limit"
            )
    return CompiledOutputSchema(
        schema_json,
        annotations_json,
        tuple(compiler.mappings) if complete else (),
        tuple(compiler.items),
        Report(
            "jsonschema.translation",
            len(compiler.items),
            tuple(compiler.issues),
            requested=max(1, len(compiler.items) + int(not complete)),
            complete=complete,
            children=tuple(compiler.children),
        ),
    )
