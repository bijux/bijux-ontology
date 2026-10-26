"""Project finite scalar and nested value alternatives with explicit RDF losses."""

from collections.abc import Callable

from rdflib.namespace import XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import (
    retain_report,
)
from bijux_ontology.json.paths import bound_index

from ..preparation import native_scalar
from .state import SchemaProjection


def project_constraints(
    projection: SchemaProjection,
    schema: dict[str, object],
    value: ValueDeclaration,
    model: str,
    name: str,
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
            bound_index(number, projection.budget)
            inclusive = (
                selected.minimum_inclusive
                if key == "minimum"
                else selected.maximum_inclusive
            )
            keyword = key if inclusive else "exclusive" + key.title()
            schema[keyword] = number
            projection.record(
                model, name, key, "Integral native JSON numeric bound is preserved"
            )
        else:
            projection.record(
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
            projection.record(
                model, name, key, "Original Unicode string length is preserved"
            )
        else:
            projection.record(
                model,
                name,
                key,
                "String-length semantics are unavailable for this encoding",
                supported=False,
            )
    if selected.pattern is not None:
        projection.record(
            model,
            name,
            "pattern",
            "RE2 patterns are not silently translated to JSON Schema regex syntax",
            supported=False,
        )
    if selected.iri_namespaces:
        projection.record(
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
                and id(enumeration.report) not in projection.retained
            ):
                retain_report(enumeration.report, projection.budget, projection.issues)
                projection.children.append(enumeration.report)
                projection.retained.add(id(enumeration.report))
            if not enumeration.qualified:
                projection.record(
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
            values = [native_scalar(term, projection.budget) for term in terms]
        except LimitExceeded:
            raise
        except InputError:
            projection.record(
                model,
                name,
                "enumeration",
                "RDF lexical identity has no exact native JSON enumeration",
                supported=False,
            )
        else:
            projection.record(
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


def project_branch(
    projection: SchemaProjection,
    value: ValueDeclaration,
    model: str,
    field: str,
    active: frozenset[str],
    project_nested: Callable[[SchemaProjection, str, frozenset[str]], None],
) -> object:
    """Translate one declared alternative without dropping unsupported branches."""
    projection.budget.consume("states", 2)
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
            projection.record(
                model,
                field,
                "datatype",
                "Datatype has no supported native JSON representation",
                supported=False,
            )
            return {}
        if value.datatype == str(XSD.integer):
            projection.record(
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
            projection.record(
                model,
                field,
                "literal metadata",
                "Language, direction and context coercion remain RDF obligations",
                supported=False,
            )
        projection.record(
            model,
            field,
            "value branch",
            "Explicit native literal representation is preserved",
        )
    elif value.kind in ("iri", "object"):
        if value.model is not None and value.kind == "object":
            if value.model in active or value.model not in projection.models:
                projection.record(
                    model,
                    field,
                    "nested model",
                    "Recursive or absent output models remain untranslated",
                    supported=False,
                )
                result = {"type": "object"}
            else:
                project_nested(projection, value.model, active)
                result = {"$ref": projection.reference(value.model)}
        else:
            result = {"type": "string" if value.kind == "iri" else "object"}
        projection.record(
            model,
            field,
            "RDF resource",
            "Resource identity, typing and context remain RDF obligations",
            supported=False,
        )
    else:
        projection.record(
            model,
            field,
            "value branch",
            "Unknown RDF kind cannot constrain the serialized JSON branch",
            supported=False,
        )
        return {}
    if value.types or value.role is not None:
        projection.record(
            model,
            field,
            "resource typing",
            "RDF class and role obligations have no JSON-only proof",
            supported=False,
        )
    if value.samples:
        projection.record(
            model,
            field,
            "authored literal samples",
            "Authored RDF samples are not JSON value enumerations",
            supported=False,
        )
    return project_constraints(projection, result, value, model, field)
