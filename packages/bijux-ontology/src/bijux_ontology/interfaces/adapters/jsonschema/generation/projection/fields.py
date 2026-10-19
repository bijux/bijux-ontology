"""Choose one field representation and retain independent output obligations."""

from collections.abc import Callable
from dataclasses import dataclass

from rdflib.namespace import XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
)

from .scalars import project_branch
from .state import SchemaProjection


@dataclass(frozen=True, slots=True)
class FieldRepresentation:
    """One selected field and native encoding shared by schema and RDF sidecar."""

    field: FieldDeclaration
    shape: str
    native_output: bool


def select_representation(
    projection: SchemaProjection, model: str, field: FieldDeclaration
) -> FieldRepresentation:
    """Prepay one finite adapter-local decision before either output is built."""
    projection.budget.consume("states")
    projection.budget.reserve_storage(128)
    return FieldRepresentation(
        field,
        projection.representations.get((model, field.name), "scalar"),
        projection.profile == "native-jsonld",
    )


def project_field(
    projection: SchemaProjection,
    model: str,
    decision: FieldRepresentation,
    active: frozenset[str],
    project_nested: Callable[[SchemaProjection, str, frozenset[str]], None],
) -> object:
    """Keep source presence, native value shape and RDF counts separate."""
    field = decision.field
    if not decision.native_output:
        projection.record(
            model,
            field.name,
            "serializer representation",
            "Custom output encoding has no translated native value constraints",
            supported=False,
        )
        return {}
    shape = decision.shape
    listed = all(value.container == "@list" for value in field.values)
    if listed != (shape == "list") or any(
        value.container not in (None, "@set", "@list") for value in field.values
    ):
        projection.record(
            model,
            field.name,
            "container",
            "Selected JSON shape does not qualify the declared RDF container",
            supported=False,
        )
        return {}
    branches = [
        project_branch(projection, value, model, field.name, active, project_nested)
        for value in field.values
    ]
    schema: object = branches[0] if len(branches) == 1 else {"anyOf": branches}
    requirement = field.output
    if shape in ("array", "list"):
        projection.record(
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
                        projection.record(
                            model,
                            field.name,
                            "list member positions",
                            "Nonnull list positions preserve the declared count",
                        )
                    elif count is not None:
                        projection.record(
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
            projection.record(
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
                projection.record(
                    model,
                    field.name,
                    "distinct RDF values",
                    "Nonnull native scalar encoding contributes one RDF value",
                )
            else:
                projection.record(
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
        projection.record(
            model,
            field.name,
            "null/empty output",
            "Explicit null/empty source permissions are retained",
        )
    else:
        projection.record(
            model,
            field.name,
            "null/empty output",
            "No explicit null/empty output contract was supplied",
            supported=False,
        )
    return schema
