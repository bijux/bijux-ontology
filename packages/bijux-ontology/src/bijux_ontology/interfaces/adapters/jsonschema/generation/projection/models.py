"""Compose finite model definitions and linked mappings from the same field choices."""

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.jsonschema.types import SchemaModel

from ..preparation import owned_values
from .fields import project_field, select_representation
from .state import SchemaProjection


def discriminated_roots(projection: SchemaProjection, roots: tuple[str, ...]) -> bool:
    """Use oneOf only for mutually exclusive required scalar string constants."""
    first = projection.definitions[roots[0]]
    if not isinstance(first, dict):
        return False
    for key in first.get("required", ()):
        projection.budget.consume("work")
        tags: set[str] = set()
        for name in roots:
            projection.budget.consume("work")
            definition = projection.definitions[name]
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
            projection.budget.consume("states")
            tags.add(tag)
        else:
            return True
    return False


def project_model(
    projection: SchemaProjection, identifier: str, active: frozenset[str]
) -> None:
    """Emit one local definition and its separately linked RDF mapping."""
    if identifier in projection.definitions:
        return
    if len(active) >= projection.budget.limits.max_depth:
        raise LimitExceeded("Nested output models exceed max_depth")
    selected = projection.models[identifier]
    properties: dict[str, object] = {}
    required = []
    mappings = []
    requirements = []
    field_annotations: dict[str, object] = {}
    annotations: dict[str, object] = {
        "types": list(selected.types),
        "fields": field_annotations,
    }
    projection.record(
        identifier,
        None,
        "output object",
        "Explicit model object output is retained",
    )
    for field in selected.fields:
        projection.budget.consume("states", 4)
        key = field.key or field.name
        if key in properties:
            raise InputError("Output model contains conflicting serialized keys")
        decision = select_representation(projection, identifier, field)
        properties[key] = project_field(
            projection, identifier, decision, active | {identifier}, project_model
        )
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
            projection.record(
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
                    owned_values(decision.field.values, projection.budget),
                    reverse=field.reverse,
                    emission=field.emission,
                    output_contract=decision.native_output,
                ),
            )
        )
        if field.output is not None:
            requirements.append((key, field.output))
        field_annotations[key] = {
            "predicate": field.predicate,
            "reverse": field.reverse,
            "label": projection.labels.get(field.predicate),
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
    projection.budget.consume("states", 3)
    projection.definitions[identifier] = result
    projection.annotations[identifier] = annotations
    projection.mappings.append(
        SchemaModel(
            projection.iri + projection.reference(identifier),
            identifier,
            selected.types,
            tuple(mappings),
            tuple(requirements),
        )
    )
    projection.record(
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
        projection.record(
            identifier,
            None,
            "subject identity/types",
            "RDF subject identity and asserted types remain linked annotations",
            supported=False,
        )
    if selected.coverage is not None or selected.emission_groups:
        projection.record(
            identifier,
            None,
            "scope/scenarios",
            "RDF coverage and emission scenarios remain untranslated",
            supported=False,
        )
