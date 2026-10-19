"""Finite JSON declarations of the existing typed selector model."""

from typing import cast

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.json.values import (
    array_value,
    boolean_value,
    known_keys,
    object_value,
    optional_string_value,
    string_value,
    string_values,
)
from bijux_ontology.jsonld.selectors.types import (
    ArrayItems,
    CoverageUnit,
    EntitySelector,
    KeyedItems,
    Property,
    SourceSelector,
    Step,
)


def selector_from_data(value: object) -> SourceSelector | EntitySelector:
    """Decode only typed finite paths, policies and explicit graph scope."""
    data = object_value(value, location="selector")
    steps: list[Step] = []
    for index, raw in enumerate(array_value(data.get("steps", []))):
        where = f"selector/steps/{index}"
        item = object_value(raw, location=where)
        kind = item.get("kind")
        common = {"kind", "minimum", "maximum"}
        minimum = cast(int, item.get("minimum", 0))
        maximum = cast(
            int | None, item.get("maximum", 1 if kind == "property" else None)
        )
        if kind == "property":
            known_keys(item, common | {"key"}, location=where)
            steps.append(Property(string_value(item.get("key")), minimum, maximum))
        elif kind == "array":
            known_keys(item, common, location=where)
            steps.append(ArrayItems(minimum, maximum))
        elif kind == "keyed":
            known_keys(item, common | {"keys"}, location=where)
            keys = None if item.get("keys") is None else string_values(item["keys"])
            steps.append(KeyedItems(keys, minimum, maximum))
        else:
            raise InputError("Unsupported selector step", location=where)
    minimum = cast(int, data.get("minimum", 1))
    maximum = cast(int | None, data.get("maximum"))
    common = {"kind", "steps", "minimum", "maximum"}
    if data.get("kind") == "source":
        known_keys(data, common | {"start"}, location="selector")
        return SourceSelector(
            tuple(steps),
            tuple(cast(list[str | int], array_value(data.get("start", [])))),
            minimum=minimum,
            maximum=maximum,
        )
    if data.get("kind") == "entity":
        known_keys(
            data,
            common
            | {
                "identifiers",
                "types",
                "graph",
                "coverage_unit",
                "exact_types",
                "include_references",
            },
            location="selector",
        )
        if "graph" not in data:
            raise InputError("Entity selection requires explicit graph scope")
        return EntitySelector(
            string_values(data.get("identifiers", [])),
            string_values(data.get("types", [])),
            graph=optional_string_value(data["graph"]),
            coverage_unit=cast(
                CoverageUnit, string_value(data.get("coverage_unit", "rdf_entity"))
            ),
            exact_types=boolean_value(data.get("exact_types", False)),
            include_references=boolean_value(data.get("include_references", False)),
            steps=tuple(steps),
            minimum=minimum,
            maximum=maximum,
        )
    raise InputError("Unsupported selector kind", location="selector")
