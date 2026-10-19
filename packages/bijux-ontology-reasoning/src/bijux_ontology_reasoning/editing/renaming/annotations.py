"""Bounded caller-authored annotation-position interpretation."""

from collections.abc import Sequence

from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget

from ...documents.sources.native import native_iri
from .types import AnnotationRole


def annotation_policy(
    values: Sequence[AnnotationRole], budget: Budget
) -> list[dict[str, str]]:
    """Require distinct explicit positions; no annotation role is inferred."""
    selected = capture_items(values, AnnotationRole, budget)
    budget.consume("bytes", 128 + len(selected) * 256)
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in selected:
        if type(value) is not AnnotationRole:
            raise InputError("Annotation policies require ordinary typed records")
        property_iri = value.property
        position: object = value.position
        action: object = value.action
        iri = native_iri(property_iri, budget)
        if type(position) is not str or position not in (
            "subject",
            "value",
            "domain",
            "range",
        ):
            raise InputError("Choose an annotation subject, value, domain or range")
        if type(action) is not str or action not in ("preserve", "rename"):
            raise InputError("Annotation positions need preserve or rename")
        budget.consume("states", 2)
        key = (iri, position)
        if key in seen:
            raise InputError("Annotation positions cannot have competing policies")
        seen.add(key)
        result.append({"property": iri, "position": position, "action": action})
    return result
