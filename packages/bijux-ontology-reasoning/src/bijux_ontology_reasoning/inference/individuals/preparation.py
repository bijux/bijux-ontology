"""Own bounded typed requests before native work or caller callbacks."""

from collections.abc import Sequence
from typing import cast

from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text

from ...axioms.types import DataProperty, NamedEntity, ObjectProperty
from .types import IndividualFamily, IndividualQuery


def _iri(value: object, operation: Budget) -> str:
    """Bound plain text before validating or retaining a role-qualified IRI."""
    if type(value) is not str:
        raise InputError("Individual query IRIs require plain text")
    bound_text(value, operation)
    return str(absolute_iri(value))


def capture_queries(
    queries: Sequence[IndividualQuery], operation: Budget
) -> tuple[tuple[IndividualQuery, ...], list[dict[str, object]]]:
    """Copy scalar fields into new immutable records and exact native requests."""
    selected = capture_items(queries, IndividualQuery, operation)
    if not selected:
        raise InputError("No individual queries were supplied")
    operation.consume("states", len(selected) * 14)
    operation.consume("bytes", len(selected) * 2048)
    owned: list[IndividualQuery] = []
    wire: list[dict[str, object]] = []
    for query in selected:
        family = cast(object, query.family)
        if type(family) is not str:
            raise InputError("Individual query family requires plain text")
        bound_text(family, operation)
        if family not in (
            "types",
            "instances",
            "object_values",
            "data_values",
            "same",
            "different",
        ):
            raise InputError("Choose a supported individual query family")
        if (family in ("types", "instances") and type(query.direct) is not bool) or (
            family not in ("types", "instances") and query.direct is not None
        ):
            raise InputError("Select explicit depth only for types and instances")
        focus = query.focus
        role = "class" if family == "instances" else "named_individual"
        if type(focus) is not NamedEntity:
            raise InputError("Individual query focus requires a typed named entity")
        kind = cast(object, focus.kind)
        if type(kind) is not str:
            raise InputError("Individual query roles require plain text")
        bound_text(kind, operation)
        if focus.kind != role:
            raise InputError("Individual query focus has a different entity role")
        focus = NamedEntity(focus.kind, _iri(focus.iri, operation))
        prop = query.property
        encoded_property: dict[str, object] | None = None
        if family == "object_values":
            if type(prop) is not ObjectProperty or type(prop.inverse) is not bool:
                raise InputError("Object values require an explicit object property")
            prop = ObjectProperty(_iri(prop.iri, operation), prop.inverse)
            encoded_property = {
                "kind": "object_property",
                "iri": prop.iri,
                "inverse": prop.inverse,
            }
        elif family == "data_values":
            if type(prop) is not DataProperty:
                raise InputError("Data values require an explicit data property")
            prop = DataProperty(_iri(prop.iri, operation))
            encoded_property = {"kind": "data_property", "iri": prop.iri}
        elif prop is not None:
            raise InputError("Only property-value queries select a property")
        owned.append(
            IndividualQuery(cast(IndividualFamily, family), focus, prop, query.direct)
        )
        wire.append(
            {
                "family": family,
                "focus": {"iri": focus.iri, "role": role},
                "property": encoded_property,
                "direct": query.direct,
            }
        )
    return tuple(owned), wire
