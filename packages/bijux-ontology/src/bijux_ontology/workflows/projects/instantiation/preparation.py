"""Qualify explicit parameter bindings and compile only the declared shape slots."""

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast

from rdflib import Literal as RdfLiteral
from rdflib import URIRef

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.archive import resource_path
from bijux_ontology.documents.export import export_rdf
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclValue,
)
from bijux_ontology.workflows.projects.profiles.composition import compose_profile
from bijux_ontology.workflows.projects.profiles.execution import EffectiveProfile
from bijux_ontology.workflows.projects.profiles.loading import _items, _text
from bijux_ontology.workflows.projects.profiles.types import ValidationProfile

from .types import (
    ConstraintSlot,
    InventorySlot,
    ParameterizedProfile,
    ParameterValue,
    ProfileParameter,
    ProfileResource,
    ProfileShape,
    ResourceSlot,
)

_NodeKind = Literal[
    "iri", "blank", "literal", "blank_or_iri", "blank_or_literal", "iri_or_literal"
]


_SlotAttribute = Literal[
    "has_value",
    "in_values",
    "class_iri",
    "datatype",
    "node_kind",
    "minimum",
    "maximum",
]


class InstantiationUnavailable(InputError):
    """Required finite representation or baseline correspondence is unestablished."""


@dataclass(frozen=True)
class PreparedInstantiation:
    """Operation-owned validated writes, before any destination is created."""

    baseline: EffectiveProfile
    configuration: dict[str, object]
    shapes: dict[str, tuple[str, bytes]]
    inventories: dict[str, bytes]
    resources: dict[str, ProfileResource]


def _bindings(
    template: ParameterizedProfile,
    supplied: Mapping[str, ParameterValue],
    budget: Operation,
) -> tuple[dict[str, ProfileParameter], dict[str, ParameterValue]]:
    """Capture every typed value before acquiring or writing the profile artifact."""
    if not isinstance(supplied, Mapping):
        raise InputError("Profile bindings require a finite mapping")
    declarations = {}
    for raw in _items(template.parameters, budget):
        if not isinstance(raw, ProfileParameter):
            raise InputError("Expected typed profile parameter declarations")
        name = _text(raw.name, budget)
        if name in declarations or raw.kind not in (
            "rdf_term",
            "integer",
            "enum",
            "resource",
        ):
            raise InputError(
                "Profile parameter names and kinds must be explicit and unique"
            )
        choices = tuple(_text(item, budget) for item in _items(raw.choices, budget))
        if len(set(choices)) != len(choices) or bool(choices) != (raw.kind == "enum"):
            raise InputError("Only enum parameters require nonempty unique choices")
        declarations[name] = replace(raw, name=name, choices=choices)
    if not declarations:
        raise InputError("A parameterized profile requires declared parameters")
    count = len(supplied)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    values: dict[str, ParameterValue] = {}
    for index, (key, value) in enumerate(supplied.items()):
        budget.consume("work")
        if index >= count:
            raise InputError("Profile bindings changed during capture")
        key = _text(key, budget)
        parameter = declarations.get(key)
        if parameter is None or key in values:
            raise InputError("Undeclared or duplicated profile parameter")
        if parameter.kind == "rdf_term":
            if not isinstance(value, URIRef | RdfLiteral):
                raise InputError(
                    "RDF-term parameters require explicit URIRef or Literal"
                )
            bound_node(value, budget)
            if isinstance(value, URIRef):
                _text(value, budget, iri=True)
        elif parameter.kind == "integer":
            if type(value) is not int:
                raise InputError("Integer parameters exclude booleans and coercion")
            if value.bit_length() > min(4096, budget.limits.max_bytes):
                raise InstantiationUnavailable(
                    "Integer parameter representation is unsupported"
                )
            bound_text(str(value), budget)
        elif parameter.kind == "enum":
            if type(value) is not str or _text(value, budget) not in parameter.choices:
                raise InputError("Enum value is outside the declared exact choices")
        else:
            if not isinstance(value, ProfileResource):
                raise InputError("Resource parameters require an owned ProfileResource")
            from bijux_ontology.workflows.projects.bundles import PreparedBundle

            if not isinstance(value.bundle, PreparedBundle):
                raise InputError("Resource selection requires a prepared bundle")
            alias = _text(value.source, budget)
            if alias not in value.bundle.rdf:
                raise InputError("Selected resource must be a local declared RDF alias")
            for descriptor in value.bundle.rdf.values():
                budget.consume("work")
                if not isinstance(descriptor.content, bytes):
                    raise InputError(
                        "Selected bundles require owned serialized RDF bytes"
                    )
            for payload in value.bundle.files.values():
                budget.consume("work")
                if not isinstance(payload, bytes):
                    raise InputError("Selected bundle members must be immutable bytes")
            terms = tuple(
                _text(term, budget, iri=True) for term in _items(value.terms, budget)
            )
            if len(set(terms)) != len(terms):
                raise InputError("Selected resource terms must be unique named IRIs")
            value = ProfileResource(value.bundle, alias, terms)
        values[key] = value
    if len(values) != count or values.keys() != declarations.keys():
        raise InputError(
            "Every declared profile parameter must be supplied exactly once"
        )
    return declarations, values


def _value(slot: ConstraintSlot, value: ParameterValue, budget: Operation) -> object:
    """Qualify only the explicitly supported attribute/type combinations."""
    budget.consume("work")
    budget.consume("states")
    if slot.attribute in ("minimum", "maximum") and type(value) is int:
        return value
    if slot.attribute == "has_value" and isinstance(value, URIRef | RdfLiteral):
        bound_node(value, budget)
        return value
    if slot.attribute in ("class_iri", "datatype") and isinstance(value, URIRef):
        bound_node(value, budget)
        return str(value)
    if slot.attribute == "node_kind" and type(value) is str:
        return value
    if slot.attribute == "in_values" and isinstance(value, ProfileResource):
        budget.consume("states", len(value.terms))
        budget.consume("bytes", 64 + 16 * len(value.terms))
        terms = []
        for term in value.terms:
            bound_text(term, budget)
            terms.append(URIRef(term))
        return tuple(terms)
    raise InputError("Parameter type is incompatible with its declared constraint slot")


def prepare_instantiation(
    template: ParameterizedProfile,
    supplied: Mapping[str, ParameterValue],
    parents: Mapping[str, ValidationProfile],
    budget: Operation,
    issues: IssueCollector,
) -> PreparedInstantiation:
    """Check all slots and exact baseline structure before preparing bounded writes."""
    if not isinstance(template, ParameterizedProfile) or not isinstance(
        template.profile, ValidationProfile
    ):
        raise InputError("Expected a typed parameterized ordinary profile")
    declarations, values = _bindings(template, supplied, budget)
    shape_specs: dict[str, ProfileShape] = {}
    for spec in _items(template.shapes, budget):
        if not isinstance(spec, ProfileShape):
            raise InputError("Expected typed profile shapes")
        alias = _text(spec.resource, budget)
        if alias in shape_specs:
            raise InputError("Shape resources cannot have multiple authored baselines")
        shape_specs[alias] = spec
    constraints: dict[str, list[ConstraintSlot]] = {}
    assigned: set[tuple[object, ...]] = set()
    used: set[str] = set()
    for slot in _items(template.constraints, budget):
        if not isinstance(slot, ConstraintSlot):
            raise InputError("Expected typed constraint slots")
        parameter = _text(slot.parameter, budget)
        alias = _text(slot.resource, budget)
        attribute = _text(slot.attribute, budget)
        if attribute not in (
            "has_value",
            "in_values",
            "class_iri",
            "datatype",
            "node_kind",
            "minimum",
            "maximum",
        ):
            raise InputError("Unsupported finite constraint slot attribute")
        slot = replace(
            slot,
            parameter=parameter,
            resource=alias,
            attribute=cast(_SlotAttribute, attribute),
        )
        if parameter not in declarations or alias not in shape_specs:
            raise InputError(
                "Constraint slot references an undeclared parameter or resource"
            )
        if not isinstance(slot.shape, URIRef):
            raise InstantiationUnavailable(
                "Constraint slots require named shape identities"
            )
        _text(slot.shape, budget, iri=True)
        if (
            type(slot.constraint) is not int
            or slot.constraint < 0
            or slot.constraint > budget.limits.max_states
        ):
            raise InputError("Constraint slot requires a bounded nonnegative index")
        key = alias, slot.shape, slot.constraint, attribute
        if key in assigned:
            raise InputError("Multiple parameters cannot overwrite one constraint slot")
        assigned.add(key)
        _value(slot, values[parameter], budget)
        constraints.setdefault(alias, []).append(slot)
        used.add(parameter)
    inventories: dict[str, bytes] = {}
    for slot in _items(template.inventories, budget):
        if not isinstance(slot, InventorySlot):
            raise InputError("Expected typed inventory slots")
        parameter = _text(slot.parameter, budget)
        path = resource_path(_text(slot.path, budget))
        value = values.get(parameter)
        if path in ("project.json", "profile.json"):
            raise InputError("Inventory slots cannot overwrite profile control files")
        if not isinstance(value, ProfileResource) or path in inventories:
            raise InputError(
                "Inventory slots require distinct paths and resource parameters"
            )
        budget.consume("bytes", 4 * len(value.terms) + 2)
        for term in value.terms:
            bound_text(term, budget)
            budget.consume("bytes", 3 * len(term.encode()))
        payload = json.dumps(value.terms, ensure_ascii=False).encode()
        if len(payload) > budget.limits.max_bytes:
            raise InstantiationUnavailable("Generated inventory exceeds max_bytes")
        inventories[path] = payload
        used.add(parameter)
    resources: dict[str, ProfileResource] = {}
    for slot in _items(template.resources, budget):
        if not isinstance(slot, ResourceSlot):
            raise InputError("Expected typed resource slots")
        parameter, alias = _text(slot.parameter, budget), _text(slot.alias, budget)
        value = values.get(parameter)
        if (
            not isinstance(value, ProfileResource)
            or alias in resources
            or alias in shape_specs
        ):
            raise InputError(
                "Resource slots require distinct non-shape aliases "
                "and resource parameters"
            )
        resources[alias] = value
        used.add(parameter)
    if used != declarations.keys():
        raise InputError("Every declared parameter must drive an explicit slot")
    baseline = compose_profile(
        template.profile,
        parents,
        budget=budget,
        timeout=max(0.001, budget.deadline - time.monotonic()),
        cancel=budget.cancel,
    )
    retain_report(baseline.report, budget, issues)
    if not baseline.report.verified:
        raise InstantiationUnavailable(
            "Original mandatory profile composition is unverified"
        )
    root = next(layer for layer in baseline.layers if layer.iri == template.profile.iri)
    assert root.bundle is not None
    config = root.bundle.configuration.to_dict()
    rdf = object_value(config.get("rdf", {}), location="rdf")
    for alias in resources:
        if alias not in rdf:
            raise InputError("Resource slots can only rebind existing local aliases")
    inventory_paths = {
        resource_path(job["inventory"])
        for job in root.bundle.configuration.checks()
        if job.get("kind") == "vocabulary" and "inventory" in job
    }
    if inventories.keys() - inventory_paths:
        raise InputError(
            "Inventory slots require registered ordinary vocabulary inventories"
        )
    shared_inputs = {
        resource_path(object_value(entry, location="rdf")["path"])
        for entry in rdf.values()
    }
    shared_inputs.update(
        resource_path(path)
        for path in object_value(
            config.get("contexts", {}), location="contexts"
        ).values()
    )
    if inventories.keys() & shared_inputs:
        raise InstantiationUnavailable(
            "Inventory replacement has an additional RDF/context role"
        )
    exports = {}
    for alias, spec in shape_specs.items():
        budget.consume("work")
        descriptor = root.bundle.rdf.get(alias)
        if descriptor is None:
            raise InputError("Shape baseline requires an existing local RDF resource")
        original = prepare_source(
            descriptor, budget=budget, document_loader=root.bundle.document_loader
        )
        if (
            original.is_dataset
            or descriptor.union
            or descriptor.graph_name not in (None, "@default")
        ):
            raise InstantiationUnavailable(
                "Dataset shape replacement requires qualified graph preservation"
            )
        budget.consume("states", len(spec.definitions))
        budget.consume("work", len(spec.definitions))
        budget.consume("bytes", 64 + 16 * len(spec.definitions))
        definitions = list(spec.definitions)
        for definition in definitions:
            budget.consume("work")
            if type(definition) not in (
                ShaclNodeShape,
                ShaclPropertyShape,
            ) or not isinstance(definition.identifier, URIRef):
                raise InstantiationUnavailable(
                    "Authored baselines require named shape definitions"
                )
        compiled = compile_shapes(spec.definitions, budget=budget)
        comparison = compare_rdf(
            original,
            compiled,
            budget=budget,
            timeout_seconds=max(0.001, budget.deadline - time.monotonic()),
        )
        retain_report(comparison, budget, issues)
        if not comparison.verified:
            raise InstantiationUnavailable(
                "Authored baseline does not preserve the complete original shapes graph"
            )
        positions = {
            definition.identifier: index for index, definition in enumerate(definitions)
        }
        changed_constraints = {}
        for slot in constraints.get(alias, []):
            budget.consume("work")
            index = positions.get(slot.shape)
            if index is None:
                raise InputError("Constraint slot references an absent named shape")
            definition = definitions[index]
            if slot.constraint >= len(definition.constraints):
                raise InputError("Constraint slot references an absent constraint")
            if index not in changed_constraints:
                count = len(definition.constraints)
                budget.consume("states", count)
                budget.consume("work", count)
                budget.consume("bytes", 64 + 16 * count)
                changed_constraints[index] = list(definition.constraints)
            owned = changed_constraints[index]
            constraint = owned[slot.constraint]
            expected = (
                ShaclCount if slot.attribute in ("minimum", "maximum") else ShaclValue
            )
            if type(constraint) is not expected:
                raise InputError(
                    "Constraint slot attribute has the wrong typed constraint"
                )
            slot_value = _value(slot, values[slot.parameter], budget)
            typed_constraint = constraint
            changed: ShaclCount | ShaclValue
            if isinstance(typed_constraint, ShaclCount):
                changed = replace(
                    typed_constraint,
                    minimum=cast(int, slot_value)
                    if slot.attribute == "minimum"
                    else typed_constraint.minimum,
                    maximum=cast(int, slot_value)
                    if slot.attribute == "maximum"
                    else typed_constraint.maximum,
                )
            else:
                changed = replace(
                    typed_constraint,
                    has_value=cast(URIRef | RdfLiteral, slot_value)
                    if slot.attribute == "has_value"
                    else typed_constraint.has_value,
                    in_values=cast(tuple[URIRef, ...], slot_value)
                    if slot.attribute == "in_values"
                    else typed_constraint.in_values,
                    class_iri=cast(str, slot_value)
                    if slot.attribute == "class_iri"
                    else typed_constraint.class_iri,
                    datatype=cast(str, slot_value)
                    if slot.attribute == "datatype"
                    else typed_constraint.datatype,
                    node_kind=cast("_NodeKind", slot_value)
                    if slot.attribute == "node_kind"
                    else typed_constraint.node_kind,
                )
            owned[slot.constraint] = changed
        for index, owned in changed_constraints.items():
            definitions[index] = replace(definitions[index], constraints=tuple(owned))
        bound = compile_shapes(tuple(definitions), budget=budget)
        syntax = {"ttl": "turtle", "rdf": "xml"}.get(
            descriptor.format or "turtle", descriptor.format or "turtle"
        )
        exported = export_rdf(
            bound,
            format=syntax,
            budget=budget,
            timeout_seconds=max(0.001, budget.deadline - time.monotonic()),
        )
        retain_report(exported.report, budget, issues)
        if exported.content is None or not exported.report.verified:
            raise InstantiationUnavailable(
                "Bound shape graph has no faithful ordinary RDF export"
            )
        exports[alias] = syntax, exported.content
    return PreparedInstantiation(baseline, config, exports, inventories, resources)
