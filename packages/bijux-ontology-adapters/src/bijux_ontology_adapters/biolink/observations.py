"""Bounded native Biolink element, ancestry, slot and rule observations."""

from typing import Any, cast

from bijux_ontology.execution.limits import Budget, bound_text

from .preparation import reserve

_UNSUPPORTED = (
    "any_of",
    "all_of",
    "exactly_one_of",
    "none_of",
    "equals_string",
    "equals_number",
    "equals_expression",
    "equals_string_in",
    "pattern",
    "structured_pattern",
    "minimum_value",
    "maximum_value",
    "range_expression",
    "has_member",
    "all_members",
    "array",
    "values_from",
)


def class_ancestors(toolkit: Any, name: str, budget: Budget) -> list[str]:
    """Retain maintained finite class ancestry; no OWL consequence is asserted."""
    budget.consume("work", len(toolkit.view.all_classes()) + 1)
    values = toolkit.view.class_ancestors(name, mixins=True, reflexive=True)
    reserve(budget, len(values))
    for value in values:
        bound_text(value, budget)
    return cast(list[str], values)


def slot_facets(slot: Any, budget: Budget) -> dict[str, object]:
    """Keep the effective native facets relevant to this selected application check."""
    reserve(budget)
    result = {
        "name": str(slot.name),
        "range": slot.range,
        "domain": slot.domain,
        "subproperty_of": slot.subproperty_of,
        "required": slot.required,
        "multivalued": slot.multivalued,
        "minimum_cardinality": slot.minimum_cardinality,
        "maximum_cardinality": slot.maximum_cardinality,
    }
    for value in result.values():
        if isinstance(value, str):
            bound_text(value, budget)
    unsupported = [name for name in _UNSUPPORTED if getattr(slot, name, None)]
    reserve(budget, len(unsupported))
    result["unsupported"] = unsupported
    return result


def rule_slots(value: Any, budget: Budget) -> set[str]:
    """Inspect native rule field references without inventing rule evaluation."""
    budget.consume("work")
    result: set[str] = set()
    if value is None:
        return result
    conditions = getattr(value, "slot_conditions", {})
    for name in conditions:
        bound_text(str(name), budget)
        reserve(budget)
        result.add(str(name))
    for field in ("any_of", "all_of", "none_of", "exactly_one_of"):
        for child in getattr(value, field, ()) or ():
            result.update(rule_slots(child, budget))
    return result


def resolve_element(toolkit: Any, value: str, budget: Budget) -> Any:
    """Obtain the actual native element under its original finite lookup charge."""
    budget.consume(
        "work", len(toolkit.view.all_classes()) + len(toolkit.view.all_slots())
    )
    return toolkit.get_element(value)
