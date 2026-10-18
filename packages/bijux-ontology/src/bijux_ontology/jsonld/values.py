"""Immutable traits of values observed in actual expanded JSON-LD usages."""

from dataclasses import dataclass, field
from typing import Literal, cast

from bijux_ontology.rdf.identifiers import absolute_iri

from ..errors import InputError

ValueKind = Literal["literal", "iri", "node", "blank", "relative", "graph"]


@dataclass(frozen=True)
class ValueTraits:
    """Independent effective value traits; absence is distinct from unchecked."""

    kind: str
    datatype: str | None = None
    language: str | None = None
    identifier: str | None = None
    identifier_kind: str = "absent"
    types: tuple[str, ...] = ()
    direction: str | None = None
    index: str | None = field(default=None, kw_only=True)


def identifier_kind(value: object) -> str:
    """Distinguish actual absolute references, blank labels and unresolved values."""
    if not isinstance(value, str):
        return "absent"
    if value.startswith("_:"):
        return "blank"
    try:
        absolute_iri(value)
    except InputError:
        return "relative"
    return "iri"


def value_traits(value: object, predicate: str) -> ValueTraits:
    """Describe actual expansion independently of field declarations.

    Datatypes describe RDF conversion, including rdf:JSON and native numeric
    conversion. They do not assert that an explicitly typed lexical form is valid.
    """
    if predicate in {"@id", "@type"} and isinstance(value, str):
        return ValueTraits(
            identifier_kind(value),
            identifier=value,
            identifier_kind=identifier_kind(value),
        )
    if not isinstance(value, dict):
        return ValueTraits("unknown")
    node = cast(dict[str, object], value)
    index = node.get("@index")
    index = index if isinstance(index, str) else None
    if "@value" in node:
        datatype = node.get("@type")
        if datatype == "@json":
            datatype = "http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON"
        language = node.get("@language")
        if datatype is None:
            raw = node["@value"]
            if language:
                datatype = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
            else:
                if isinstance(raw, bool):
                    native = "boolean"
                elif isinstance(raw, (int, float)):
                    integral = isinstance(raw, int) or raw.is_integer()
                    native = "integer" if integral and abs(raw) < 10**21 else "double"
                else:
                    native = "string"
                datatype = "http://www.w3.org/2001/XMLSchema#" + native
        direction = node.get("@direction")
        return ValueTraits(
            "literal",
            str(datatype),
            str(language) if language else None,
            direction=str(direction) if direction else None,
            index=index,
        )
    identifier = node.get("@id")
    identifier = identifier if isinstance(identifier, str) else None
    types = tuple(cast(list[str], node.get("@type", [])))
    kind = (
        "graph"
        if "@graph" in node
        else identifier_kind(identifier)
        if set(node) - {"@index"} == {"@id"}
        else "node"
    )
    return ValueTraits(
        kind,
        identifier=identifier,
        identifier_kind=identifier_kind(identifier),
        types=types,
        index=index,
    )
