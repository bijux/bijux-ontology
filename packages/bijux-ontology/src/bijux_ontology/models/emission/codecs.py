"""Strict JSON-shaped source and graph-qualified entity model bindings."""

from collections.abc import Iterator
from typing import cast

from rdflib import URIRef

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import SubjectIdentity
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.values import (
    array_value,
    boolean_value,
    known_keys,
    measure_json,
    object_value,
    optional_string_value,
    string_value,
)
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage


def _records(value: object, budget: Budget) -> Iterator[tuple[str, dict[str, object]]]:
    """Prepay finite data-only bindings without copying nested input documents."""
    measure_json(value, max_bytes=budget.limits.max_bytes, budget=budget)
    for index, raw in enumerate(capture(array_value(value), budget)):
        where = f"usages/{index}"
        yield where, object_value(raw, location=where)


def usages_from_data(
    value: object, *, budget: Budget | None = None
) -> tuple[ModelUsage, ...]:
    """Decode explicit bindings without guessing paths, graphs or subject identities."""
    result = []
    allowance = budget if budget is not None else Budget()
    for where, item in _records(value, allowance):
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


def rdf_usages_from_data(
    value: object, *, budget: Budget | None = None
) -> tuple[RdfModelUsage, ...]:
    """Bind named RDF roots; configuration cannot claim owned blank-node identity."""
    allowance = budget if budget is not None else Budget()
    result = []
    for where, item in _records(value, allowance):
        known_keys(item, {"model", "subject", "location"}, location=where)
        result.append(
            RdfModelUsage(
                string_value(item.get("model")),
                URIRef(string_value(item.get("subject"))),
                location=string_value(item.get("location", where)),
            )
        )
    return tuple(result)
