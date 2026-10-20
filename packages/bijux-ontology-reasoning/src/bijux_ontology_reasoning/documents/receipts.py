"""Adopt bounded native conversion and selected-profile receipts."""

from typing import Any, cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import ResourceError
from bijux_ontology.limits import Budget

from ..backend.owlapi.protocol import result_text
from ..backend.types import OwlProfile
from .types import NativeProfile


def native_records(value: object, operation: Budget, maximum: int) -> list[Any]:
    """Admit a bounded native result inventory before inspecting its entries."""
    if not isinstance(value, list) or len(value) > maximum:
        raise ResourceError("Invalid native result inventory")
    operation.consume("states", len(value))
    operation.consume("work", len(value) + 1)
    return value


def native_profiles(
    raw: object, selected: tuple[OwlProfile, ...], operation: Budget
) -> tuple[NativeProfile, ...]:
    """Validate selected profile answers against their complete violation receipts."""
    records = native_records(raw, operation, len(selected))
    if len(records) != len(selected):
        raise ResourceError("Native worker omitted selected profiles")
    result = []
    for name, raw_record in zip(selected, records, strict=True):
        item = object_value(raw_record, location="native profile")
        profile = result_text(item.get("profile"), operation)
        answer = item.get("in_profile")
        if profile != name or type(answer) is not bool:
            raise ResourceError("Invalid native profile answer")
        violations = tuple(
            cast(str, result_text(value, operation))
            for value in native_records(
                item.get("violations"), operation, operation.limits.max_issues
            )
        )
        if bool(violations) == answer:
            raise ResourceError("Native profile answer contradicts its violations")
        result.append(NativeProfile(name, answer, violations))
    return tuple(result)
