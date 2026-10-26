"""Owned LinkML schema contracts qualified for an explicitly selected dumper."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any

from bijux_ontology.declarations.types import (
    Emission,
    FieldDeclaration,
    ModelDeclaration,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology_adapters.contracts import ModelExtraction, RdfField
from bijux_ontology_adapters.field_contracts import select_contract

if TYPE_CHECKING:
    from linkml_runtime.utils.schemaview import SchemaView  # type: ignore[import-untyped]


def _charge_tree(
    value: object,
    budget: Budget,
    depth: int = 0,
    active: set[int] | None = None,
    size: list[int] | None = None,
) -> None:
    """Bound supplied schema/instance data before copying or backend traversal."""
    if depth > budget.limits.max_depth:
        raise LimitExceeded("LinkML input exceeds max_depth")
    budget.consume("work")
    budget.consume("states")
    size = [0] if size is None else size
    if isinstance(value, str):
        amount = len(value.encode("utf-8"))
        size[0] += amount
        if size[0] > budget.limits.max_bytes:
            raise LimitExceeded("LinkML input exceeds max_bytes")
        budget.consume("bytes", amount)
        return
    if value is None or isinstance(value, (int, float, bool)):
        return
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise InputError(
            "LinkML input contains recursive objects; use named schema ranges"
        )
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                _charge_tree(key, budget, depth + 1, active, size)
                _charge_tree(item, budget, depth + 1, active, size)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _charge_tree(item, budget, depth + 1, active, size)
        elif is_dataclass(value) and not isinstance(value, type):
            for definition in fields(value):
                _charge_tree(
                    getattr(value, definition.name), budget, depth + 1, active, size
                )
        elif hasattr(value, "__dict__"):
            _charge_tree(vars(value), budget, depth + 1, active, size)
        else:
            raise InputError("Unsupported LinkML input object shape")
    finally:
        active.remove(identity)


def _schema_snapshot(view: "SchemaView", budget: Budget) -> "SchemaView":
    """Own a fully resolved effective view with a fail-closed public import hook."""
    try:
        from linkml_runtime.utils.schemaview import SchemaView
    except ImportError as error:
        raise ResourceError("Install bijux-ontology-adapters[linkml]") from error
    if not isinstance(view, SchemaView):
        raise InputError("Expected a LinkML SchemaView")
    if view.schema_map.get(view.schema.name) is not view.schema:
        raise ResourceError("LinkML root schema and resolved closure disagree")
    schemas = {}
    seen = set()
    for name, schema in view.schema_map.items():
        if getattr(schema, "default_curi_maps", None):
            raise ResourceError(
                "Supply explicit namespace prefixes; "
                "LinkML prefix-map resources are not retrieved"
            )
        budget.consume("work")
        if id(schema) not in seen:
            budget.consume("resources")
            _charge_tree(schema, budget)
            seen.add(id(schema))
        schemas[name] = schema
    root, snapshots = deepcopy((view.schema, schemas))

    class ClosedSchemaView(SchemaView):  # type: ignore[misc]
        """Permit maintained closure resolution only over supplied schema objects."""

        def load_import(self, *args: object, **kwargs: object) -> Any:
            """Never fall back to a path or network resource missing from ownership."""
            raise ResourceError(
                "Supply a fully resolved LinkML closure; "
                "adapter does not retrieve imports"
            )

    owned = ClosedSchemaView(root)
    owned.schema_map = snapshots
    owned.imports_closure()
    return owned


def _qualified_values(
    view: "SchemaView", slot: Any, identifiers: Mapping[str, str]
) -> tuple[ValueDeclaration, ...] | None:
    """Describe the maintained RDFLib dumper's explicit schema-driven term rules."""
    if any(
        getattr(slot, name, None)
        for name in (
            "any_of",
            "all_of",
            "exactly_one_of",
            "none_of",
            "array",
            "list_elements_ordered",
            "string_serialization",
            "designates_type",
        )
    ):
        return None
    range_name = slot.range
    if range_name in view.all_enums():
        enumeration = view.get_enum(range_name)
        if enumeration is None or not enumeration.permissible_values:
            return None
        values = []
        for allowed in enumeration.permissible_values.values():
            if allowed.meaning is not None:
                absolute_iri(view.expand_curie(str(allowed.meaning)))
                values.append(ValueDeclaration("iri"))
            else:
                values.append(
                    ValueDeclaration(
                        "literal", datatype="http://www.w3.org/2001/XMLSchema#string"
                    )
                )
        return tuple(dict.fromkeys(values))
    if range_name in view.all_types():
        datatype = view.get_type(range_name).uri
        if not datatype:
            return None
        if datatype in ("rdfs:Resource", "xsd:anyURI"):
            return (ValueDeclaration("iri"),)
        if datatype == "xsd:string":
            return (
                ValueDeclaration(
                    "literal", datatype="http://www.w3.org/2001/XMLSchema#string"
                ),
            )
        expanded = (
            "http://www.w3.org/2001/XMLSchema#" + str(datatype).removeprefix("xsd:")
            if str(datatype).startswith("xsd:") and "xsd" not in view.namespaces()
            else view.expand_curie(str(datatype))
        )
        return (ValueDeclaration("literal", datatype=absolute_iri(expanded)),)
    if range_name in view.all_classes():
        if slot.inlined or slot.inlined_as_list:
            targets = view.class_descendants(range_name)
            return tuple(
                ValueDeclaration("object", model=identifiers.get(str(name), str(name)))
                for name in targets
            )
        target = view.get_identifier_slot(range_name)
        return (ValueDeclaration("iri"),) if target is not None else None
    return None


def _extract(
    owned: "SchemaView",
    *,
    classes: Sequence[str] | None,
    identifiers: Mapping[str, str] | None,
    field_contracts: Mapping[str, Mapping[str, RdfField]] | None,
    serializer: str | None,
    budget: Budget,
) -> ModelExtraction:
    """Share manual and selected-serializer extraction over the same owned schema."""
    names = tuple(owned.all_classes()) if classes is None else tuple(classes)
    declarations = []
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    qualified = serializer == "rdflib"
    try:
        for name in names:
            budget.consume("work")
            cls = owned.get_class(name)
            if cls is None:
                raise InputError("Selected LinkML class is absent", location=name)
            identifier = (identifiers or {}).get(name, name)
            checked += 1
            types: tuple[str, ...]
            if cls.class_uri or qualified:
                types = (absolute_iri(owned.get_uri(cls, expand=True)),)
            else:
                types = ()
                issues.append(
                    Issue(
                        "adapter.class_mapping_unverified",
                        "LinkML class has no explicit class_uri",
                        identifier,
                        kind="unverified",
                    )
                )
            selected_fields = []
            slots = owned.class_induced_slots(name)
            id_slots = [slot for slot in slots if slot.identifier]
            subject = (
                SubjectIdentity(allow_blank=not id_slots)
                if qualified
                else SubjectIdentity()
            )
            if qualified and len(id_slots) > 1:
                complete = False
                issues.append(
                    Issue(
                        "adapter.output_unverified",
                        "Multiple identifier slots do not establish "
                        "one subject contract",
                        identifier,
                        kind="unverified",
                    )
                )
            for slot in slots:
                budget.consume("work")
                checked += 1
                location = f"{identifier}.{slot.name}"
                sidecar = (field_contracts or {}).get(name, {}).get(str(slot.name))
                if sidecar is not None and not isinstance(sidecar, RdfField):
                    raise InputError(
                        "Expected explicit RDF field contracts", location=location
                    )
                if qualified and slot.identifier:
                    if owned.is_slot_percent_encoded(slot):
                        complete = False
                        issues.append(
                            Issue(
                                "adapter.output_unverified",
                                "Percent-encoded identifier output is not qualified "
                                "as an absolute RDF IRI",
                                location,
                                kind="unverified",
                            )
                        )
                    if sidecar is not None and sidecar.predicate != "@id":
                        issues.append(
                            Issue(
                                "adapter.predicate_contract_conflict",
                                "Identifier slots emit subject identity, "
                                "not a predicate",
                                location,
                                expected="@id",
                                actual=sidecar.predicate,
                            )
                        )
                    continue
                if not slot.slot_uri and not qualified:
                    issues.append(
                        Issue(
                            "adapter.mapping_unverified",
                            "Induced slot has no explicit slot_uri",
                            location,
                            kind="unverified",
                        )
                    )
                    continue
                predicate = absolute_iri(owned.get_uri(slot, expand=True))
                values = (
                    _qualified_values(owned, slot, identifiers or {})
                    if qualified
                    else None
                )
                metadata = (
                    RdfField(predicate, values, output_contract=True)
                    if values is not None
                    else None
                )
                contract = select_contract(
                    metadata, sidecar, location=location, issues=issues
                )
                output_values: tuple[ValueDeclaration, ...]
                emission: Emission
                if contract is None or not contract.output_contract:
                    output_values = (ValueDeclaration(),)
                    reverse = False
                    emission = "unknown"
                    issues.append(
                        Issue(
                            "adapter.output_unverified",
                            "Selected serializer output is not established "
                            "for this induced slot",
                            location,
                            kind="unverified",
                        )
                    )
                    if qualified:
                        complete = False
                else:
                    output_values, reverse, emission = (
                        contract.values,
                        contract.reverse,
                        contract.emission,
                    )
                    if contract.predicate != predicate and metadata is None:
                        issues.append(
                            Issue(
                                "adapter.predicate_contract_conflict",
                                "Independent output contract differs from slot_uri",
                                location,
                                expected=predicate,
                                actual=contract.predicate,
                            )
                        )
                    predicate = contract.predicate
                selected_fields.append(
                    FieldDeclaration(
                        str(slot.name),
                        predicate,
                        output_values,
                        reverse=reverse,
                        emission=emission,
                        location=location,
                    )
                )
                if slot.alias and slot.alias != slot.name and not qualified:
                    issues.append(
                        Issue(
                            "adapter.alias_unverified",
                            "LinkML alias requires selected serializer verification",
                            location,
                            kind="unverified",
                        )
                    )
            declarations.append(
                ModelDeclaration(
                    identifier, types, tuple(selected_fields), subject=subject
                )
            )
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return ModelExtraction(
        tuple(declarations),
        Report(
            "linkml-extraction",
            checked,
            tuple(issues),
            complete=complete,
            requested=checked if complete else checked + 1,
            families=(
                "rdflib-serialization" if qualified else "explicit-serialization",
            ),
        ),
    )


def model_declarations(
    view: "SchemaView",
    *,
    classes: Sequence[str] | None = None,
    identifiers: Mapping[str, str] | None = None,
    field_contracts: Mapping[str, Mapping[str, RdfField]] | None = None,
    serializer: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ModelExtraction:
    """Extract manual contracts or opt into the maintained ``serializer='rdflib'``.

    The manual escape hatch retains caller-supplied RdfField output contracts.
    Qualified RDFLib output uses induced schema ranges and enum meanings, with
    identifier slots represented as subjects. Required input fields never imply
    guaranteed RDF emission or distinct-value minima. Supply an import-free
    schema or an explicitly resolved SchemaView; missing imports never retrieve.
    """
    if serializer not in (None, "rdflib"):
        raise InputError("Supported LinkML serializer is 'rdflib'")
    if isinstance(classes, (str, bytes)):
        raise InputError("Selected classes must be a sequence of names")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget or Budget(limits)
    try:
        _charge_tree(identifiers, operation)
        _charge_tree(field_contracts, operation)
        owned = _schema_snapshot(view, operation)
    except LimitExceeded as error:
        return ModelExtraction(
            (),
            Report(
                "linkml-extraction",
                issues=(
                    Issue("execution.limit_exceeded", str(error), kind="exhausted"),
                ),
                complete=False,
                requested=1,
            ),
        )
    return _extract(
        owned,
        classes=classes,
        identifiers=identifiers,
        field_contracts=field_contracts,
        serializer=serializer,
        budget=operation,
    )
