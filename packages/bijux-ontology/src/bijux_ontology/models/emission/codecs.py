"""Strict JSON-shaped source and graph-qualified entity model bindings."""

from typing import cast

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import SubjectIdentity
from bijux_ontology.json.values import (
    array_value,
    boolean_value,
    known_keys,
    object_value,
    optional_string_value,
    string_value,
)
from bijux_ontology.models.emission.types import ModelUsage


def usages_from_data(value: object) -> tuple[ModelUsage, ...]:
    """Decode explicit bindings without guessing paths, graphs or subject identities."""
    result = []
    for index, raw in enumerate(array_value(value)):
        where = f"usages/{index}"
        item = object_value(raw, location=where)
        known_keys(
            item,
            {"model", "path", "reference", "graph", "entity", "closed", "subject"},
            location=where,
        )
        subject = None
        if "subject" in item:
            identity = object_value(item["subject"], location=where + "/subject")
            known_keys(identity, {"expected_iri", "allow_blank"}, location=where)
            subject = SubjectIdentity(
                optional_string_value(identity.get("expected_iri")),
                allow_blank=boolean_value(identity.get("allow_blank", True)),
            )
        result.append(
            ModelUsage(
                string_value(item.get("model")),
                tuple(cast(list[str | int], array_value(item.get("path", [])))),
                subject=subject,
                reference=optional_string_value(item.get("reference")),
                graph=optional_string_value(item["graph"])
                if "graph" in item
                else ABSENT,
                entity=boolean_value(item.get("entity", False)),
                closed=boolean_value(item.get("closed", False)),
            )
        )
    return tuple(result)
