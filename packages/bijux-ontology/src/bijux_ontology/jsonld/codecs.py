"""Strict JSON-shaped declarations of selected JSON-LD usage expectations."""

from typing import Literal, cast

from bijux_ontology._json import (
    array_value,
    at_input_location,
    boolean_value,
    known_keys,
    object_value,
    optional_string_value,
    string_value,
    string_values,
)
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.expectations import ContainerKind, ContextExpectation
from bijux_ontology.jsonld.lists import ExpectedList
from bijux_ontology.jsonld.values import ValueKind as ContextValueKind


def _trait(value: object) -> str | Absent | None:
    if isinstance(value, dict) and set(value) == {"absent"} and value["absent"] is True:
        return ABSENT
    return optional_string_value(value)


def expectations_from_data(
    value: object, *, source: str = ""
) -> tuple[ContextExpectation, ...]:
    """Ingest selected usages, including explicit absence and target requirements."""
    result = []
    for index, raw in enumerate(array_value(value, source=source)):
        with at_input_location(source, (index,)):
            data = object_value(
                raw, location="", message="Expected a JSON object with string keys"
            )
            known_keys(
                data,
                {
                    "path",
                    "predicate",
                    "value_kind",
                    "datatype",
                    "language",
                    "container",
                    "reverse",
                    "identifier",
                    "types",
                    "direction",
                    "identifier_kind",
                    "ordered_values",
                    "lists",
                    "index",
                    "list_index",
                },
                location="",
                message="Unknown declaration keys",
            )
            path = array_value(data.get("path"))
            if any(type(part) not in (str, int) for part in path):
                raise InputError(
                    "Usage paths contain keys and nonnegative array indices"
                )
            result.append(
                ContextExpectation(
                    path=tuple(cast(list[str | int], path)),
                    predicate=string_value(data.get("predicate")),
                    value_kind=cast(
                        ContextValueKind | None,
                        optional_string_value(data.get("value_kind")),
                    ),
                    index=_trait(data.get("index")),
                    list_index=_trait(data.get("list_index")),
                    datatype=_trait(data.get("datatype")),
                    language=_trait(data.get("language")),
                    container=cast(
                        ContainerKind | None,
                        optional_string_value(data.get("container")),
                    ),
                    reverse=boolean_value(data.get("reverse", False)),
                    identifier=_trait(data.get("identifier")),
                    types=None
                    if data.get("types") is None
                    else string_values(data["types"]),
                    direction=cast(
                        Literal["ltr", "rtl"] | Absent | None,
                        _trait(data.get("direction")),
                    ),
                    identifier_kind=cast(
                        Literal["iri", "blank", "relative"] | None,
                        optional_string_value(data.get("identifier_kind")),
                    ),
                    lists=None
                    if data.get("lists") is None
                    else tuple(
                        ExpectedList(tuple(array_value(item)))
                        for item in array_value(data["lists"])
                    ),
                    ordered_values=None
                    if data.get("ordered_values") is None
                    else tuple(array_value(data["ordered_values"])),
                )
            )
    return tuple(result)
