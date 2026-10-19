"""Bound explicit Pydantic output metadata before registry capture or matching."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from sys import getsizeof
from typing import TypeVar

from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.terms import bound_node

_T = TypeVar("_T")


def bound_contract(value: object, budget: Budget, depth: int = 0) -> None:
    """Qualify existing immutable declaration records without encoding a schema."""
    pending = [(value, depth)]
    while pending:
        item, level = pending.pop()
        if level > budget.limits.max_depth:
            raise LimitExceeded("Pydantic contract metadata exceeds max_depth")
        budget.consume("work")
        budget.consume("states")
        if item is ABSENT or item is None or type(item) is bool:
            continue
        if isinstance(item, Node):
            bound_node(item, budget)
        elif isinstance(item, str):
            bound_text(item, budget)
        elif type(item) is int:
            budget.consume("work", item.bit_length() + 1)
            budget.consume("bytes", 1 + item.bit_length() // 8)
        elif isinstance(item, Decimal):
            size = getsizeof(item)
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Pydantic numeric metadata exceeds max_bytes")
            budget.consume("bytes", size)
            budget.consume("work", size)
        elif isinstance(item, tuple):
            budget.consume("states", len(item))
            budget.consume("bytes", 24 * len(item))
            pending.extend((child, level + 1) for child in item)
        elif is_dataclass(item) and not isinstance(item, type):
            descriptions = fields(item)
            budget.consume("states", len(descriptions))
            budget.consume("bytes", 24 * len(descriptions))
            pending.extend(
                (getattr(item, field.name), level + 1) for field in descriptions
            )
        else:
            raise InputError("Unsupported explicit Pydantic contract metadata")


def capture_mapping(value: Mapping[object, _T], budget: Budget) -> dict[object, _T]:
    """Own finite registry references; callers qualify keys before indexing them."""
    if not isinstance(value, Mapping):
        raise InputError("Pydantic contract registries require mappings")
    budget.consume("states", len(value))
    result: dict[object, _T] = {}
    for key, item in value.items():
        budget.consume("states")
        budget.consume("work")
        budget.consume("bytes", 16)
        if isinstance(key, tuple):
            if (
                len(key) != 2
                or not isinstance(key[0], type)
                or not isinstance(key[1], str)
                or isinstance(key[1], Node)
            ):
                raise InputError("Field registries require model/member pairs")
            bound_contract(key[1], budget)
            key = (key[0], str(key[1]))
        elif not isinstance(key, type):
            raise InputError("Pydantic registries require model class keys")
        if key in result:
            raise InputError("Duplicate Pydantic registry key after text normalization")
        result[key] = item
    return result


def joined(left: str, right: str, budget: Budget) -> str:
    """Reserve the complete UTF-8 anchor before concatenating its pieces."""
    bound_text(left, budget)
    bound_text(right, budget)
    if len(left) + 1 + len(right) > budget.limits.max_bytes:
        raise LimitExceeded("Pydantic contract anchor exceeds max_bytes")
    size = len(left.encode()) + 1 + len(right.encode())
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Pydantic contract anchor exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", len(left) + len(right) + 1)
    return f"{left}.{right}"
