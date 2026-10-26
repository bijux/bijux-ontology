"""Data-only sampled field associations for isolated serializer checking."""

from typing import cast

from bijux_ontology.declarations.bindings import BindingIdentity
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.paths import SourcePath, bound_path, validate_path
from bijux_ontology.json.values import (
    array_value,
    known_keys,
    object_value,
    string_value,
)
from bijux_ontology.models.emission.codecs import usages_from_data
from bijux_ontology.models.emission.observations import SerializedFieldBinding


def bindings_from_data(
    value: object, budget: Budget
) -> tuple[SerializedFieldBinding, ...]:
    """Admit finite identities and real source paths before constructing bindings."""
    values = array_value(value)
    budget.consume("states", len(values))
    budget.reserve_storage(64 + 128 * len(values))
    result = []
    for index, raw in enumerate(values):
        budget.consume("work")
        where = f"bindings/{index}"
        item = object_value(raw, location=where)
        known_keys(item, {"identity", "usage", "field", "path"}, location=where)
        identity = object_value(item.get("identity"), location=where + "/identity")
        known_keys(identity, {"owner", "path"}, location=where + "/identity")
        owner = string_value(identity.get("owner"))
        field = string_value(item.get("field"))
        bound_text(owner, budget)
        bound_text(field, budget)
        paths = []
        for raw_path in (identity.get("path"), item.get("path")):
            parts = array_value(raw_path)
            budget.consume("states", len(parts))
            budget.reserve_storage(64 + 16 * len(parts))
            path = cast(SourcePath, tuple(parts))
            validate_path(path)
            bound_path(path, budget)
            paths.append(path)
        # The protocol document is already owned and bounded. Pay the usage's
        # path before its existing decoder materialises the immutable selection.
        usage = object_value(item.get("usage"), location=where + "/usage")
        usage_path = array_value(usage.get("path", []))
        budget.consume("states", len(usage_path) + 1)
        budget.reserve_storage(256 + 16 * len(usage_path))
        for part in usage_path:
            if isinstance(part, str):
                bound_text(part, budget)
        selected = usages_from_data([usage], budget=budget)[0]
        result.append(
            SerializedFieldBinding(
                BindingIdentity(owner, paths[0]), selected, field, paths[1]
            )
        )
    return tuple(result)
