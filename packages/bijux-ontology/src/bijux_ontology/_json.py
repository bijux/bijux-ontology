"""Strict JSON input boundaries and independently owned caller values."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Limits

if TYPE_CHECKING:
    from bijux_ontology.limits import Budget


def _string_bytes(value: str) -> int:
    # Bound intermediate encoding allocations even for an already supplied string.
    return 2 + sum(
        len(json.dumps(value[start : start + 4096], ensure_ascii=False).encode("utf-8"))
        - 2
        for start in range(0, len(value), 4096)
    )


def own_json(
    value: object,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    budget: Budget | None = None,
) -> object:
    """Validate JSON size, shape and cycles before making an independent copy.

    Parsed mappings cannot reveal duplicate names discarded by another decoder.
    Size is the compact UTF-8 JSON representation, not Python object overhead.
    An optional enclosing budget also prepays traversal, container copies and
    scalar serialization workspace before allocating them. Its limits remain
    independent of any narrower explicit input-size/depth policy.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise InputError("max_bytes must be a positive integer")
    if type(max_depth) is not int or max_depth <= 0:
        raise InputError("max_depth must be a positive integer")
    if budget is not None:
        from bijux_ontology.limits import Budget as RuntimeBudget

        if not isinstance(budget, RuntimeBudget):
            raise InputError("JSON ownership requires a Budget")
        max_bytes = min(max_bytes, budget.limits.max_bytes)
        max_depth = min(max_depth, budget.limits.max_depth)
        budget.consume("bytes", 128)

    def text_workspace(text: str) -> None:
        """Reserve scalar measurement/copy and compact-serialization workspace."""
        if budget is not None:
            budget.consume("work", len(text) + 1)
            budget.consume("bytes", 64 + 16 * len(text))

    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    size = 0
    try:
        while stack:
            if budget is not None:
                budget.consume("work")
            item, depth, leaving = stack.pop()
            if leaving:
                active.remove(id(item))
                continue
            if budget is not None:
                budget.consume("states")
                budget.consume("bytes", 64)
            if depth > max_depth:
                raise LimitExceeded("JSON nesting exceeds max_depth", location=source)
            if isinstance(item, (dict, list)):
                if id(item) in active:
                    raise InputError("Cyclic containers are not JSON", location=source)
                if budget is not None:
                    budget.consume("bytes", 256 + 64 * len(item))
                active.add(id(item))
                stack.append((item, depth, True))
                size += 2 + max(0, len(item) - 1)
                if size > max_bytes:
                    raise LimitExceeded("JSON input exceeds max_bytes", location=source)
                if isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise InputError(
                                "JSON object names must be strings", location=source
                            )
                        if len(key) > max_bytes:
                            raise LimitExceeded(
                                "JSON input exceeds max_bytes", location=source
                            )
                        text_workspace(key)
                        size += _string_bytes(key) + 1
                        if size > max_bytes:
                            raise LimitExceeded(
                                "JSON input exceeds max_bytes", location=source
                            )
                        stack.append((child, depth + 1, False))
                else:
                    stack.extend((child, depth + 1, False) for child in item)
            elif isinstance(item, str):
                if len(item) > max_bytes:
                    raise LimitExceeded("JSON input exceeds max_bytes", location=source)
                text_workspace(item)
                size += _string_bytes(item)
            elif item is None or type(item) in (bool, int, float):
                if isinstance(item, float) and not math.isfinite(item):
                    raise InputError("JSON numbers must be finite", location=source)
                if budget is not None:
                    digits = (
                        max(1, (item.bit_length() * 30103) // 100000 + 1)
                        if type(item) is int
                        else 32
                    )
                    budget.consume("work", digits)
                    budget.consume("bytes", 64 + 16 * digits)
                size += len(json.dumps(item))
            else:
                raise InputError("Expected only JSON values", location=source)
            if size > max_bytes:
                raise LimitExceeded("JSON input exceeds max_bytes", location=source)
    except (UnicodeError, ValueError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError(str(error), location=source) from error
    try:
        return deepcopy(value)
    except RecursionError as error:
        raise LimitExceeded(
            "JSON copy exceeds the Python runtime nesting limit", location=source
        ) from error


class _ObjectPairs(list[tuple[str, object]]):
    """Keep duplicate names until their actual object path is known."""


def _invalid_constant(value: str) -> NoReturn:
    raise InputError(f"Non-finite number is not JSON: {value}")


@dataclass(frozen=True)
class _RawNumber:
    """Retain a maintained decoder's validated numeric token until path resolution."""

    token: str
    integer: bool


_NumberHook = Callable[[str, tuple[str | int, ...], bool], object]


def decode_json(
    payload: str | bytes,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
) -> object:
    """Decode owned JSON text, rejecting ambiguous names at every object depth."""
    return _decode_json(
        payload, source=source, max_bytes=max_bytes, max_depth=max_depth
    )


def _decode_json(
    payload: str | bytes,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    number_hook: _NumberHook | None = None,
    key_hook: Callable[[str], None] | None = None,
    budget: Budget | None = None,
) -> object:
    """Decode through shared syntax and optional private scalar/key observation."""
    if not isinstance(payload, (str, bytes)):
        raise InputError("Expected JSON text or bytes", location=source)
    try:
        size = (
            len(payload) if isinstance(payload, bytes) else len(payload.encode("utf-8"))
        )
        if size > max_bytes:
            raise LimitExceeded("JSON input exceeds max_bytes", location=source)
        data = cast(
            object,
            json.loads(
                payload,
                object_pairs_hook=_ObjectPairs,
                parse_constant=_invalid_constant,
                parse_int=(lambda token: _RawNumber(token, True))
                if number_hook is not None
                else None,
                parse_float=(lambda token: _RawNumber(token, False))
                if number_hook is not None
                else None,
            ),
        )
    except RecursionError as error:
        raise LimitExceeded(
            "JSON nesting exceeds decoder limit", location=source
        ) from error
    except (ValueError, UnicodeError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError(str(error), location=source) from error

    def pointer(parts: tuple[str | int, ...]) -> str:
        """Bound numeric-hook error pointers before escaping their components."""
        size = 1
        for part in parts:
            text = str(part)
            size += len(text.encode("utf-8")) + text.count("~") + text.count("/") + 1
            if size > max_bytes:
                raise LimitExceeded("JSON location exceeds max_bytes", location=source)
        if budget is not None:
            budget.consume("work", size)
            budget.consume("bytes", size)
        return "/" + "/".join(
            str(part).replace("~", "~0").replace("/", "~1") for part in parts
        )

    def objects(
        value: object, path: str, depth: int, parts: tuple[str | int, ...] = ()
    ) -> object:
        """Resolve object pairs with their original source paths."""
        if budget is not None:
            budget.consume("work")
            budget.consume("states")
        if depth > max_depth:
            raise LimitExceeded(
                "JSON nesting exceeds max_depth",
                location=source + (path if number_hook is None else pointer(parts)),
            )
        if isinstance(value, _ObjectPairs):
            result: dict[str, object] = {}
            for key, child in value:
                if key_hook is not None:
                    key_hook(key)
                child_parts = (*parts, key) if number_hook is not None else ()
                child_path = (
                    path + "/" + key.replace("~", "~0").replace("/", "~1")
                    if number_hook is None
                    else ""
                )
                if key in result:
                    cause = ValueError(f"Duplicate JSON object name: {key}")
                    raise InputError(
                        str(cause),
                        location=source
                        + (child_path if number_hook is None else pointer(child_parts)),
                    ) from cause
                result[key] = objects(child, child_path, depth + 1, child_parts)
            return result
        if isinstance(value, list):
            return [
                objects(
                    child,
                    f"{path}/{index}" if number_hook is None else "",
                    depth + 1,
                    (*parts, index) if number_hook is not None else (),
                )
                for index, child in enumerate(value)
            ]
        if isinstance(value, _RawNumber):
            assert number_hook is not None
            return number_hook(value.token, parts, value.integer)
        return value

    return own_json(
        objects(data, "", 0), source=source, max_bytes=max_bytes, max_depth=max_depth
    )


def object_value(
    value: object, *, location: str, message: str = "Expected an object"
) -> dict[str, object]:
    """Require a JSON object at an established input location."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InputError(message, location=location)
    return cast(dict[str, object], value)


def text_value(value: object, *, location: str) -> str:
    """Require a nonempty string without silently coercing input."""
    if not isinstance(value, str) or not value.strip():
        raise InputError("Expected a nonempty string", location=location)
    return value


def known_keys(
    value: Mapping[str, object],
    keys: set[str],
    *,
    location: str,
    message: str = "Unknown keys",
) -> None:
    """Reject ignored options instead of certifying a misspelled policy."""
    unknown = set(value) - keys
    if unknown:
        raise InputError(f"{message}: {', '.join(sorted(unknown))}", location=location)


def read_json(path: Path, limits: Limits | None = None) -> object:
    """Decode a bounded snapshot and retain its real filename in failures."""
    selected = limits or Limits()
    with path.open("rb") as handle:
        payload = handle.read(selected.max_bytes + 1)
    return decode_json(
        payload,
        source=str(path),
        max_bytes=selected.max_bytes,
        max_depth=selected.max_depth,
    )


def input_location(source: str, path: tuple[str | int, ...]) -> str:
    """Locate a parsed value by source and escaped JSON Pointer components."""
    return (
        source
        + "#"
        + "".join(
            "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
        )
        if source
        else ""
    )


@contextmanager
def at_input_location(source: str, path: tuple[str | int, ...]) -> Iterator[None]:
    """Attach source context without replacing a more precise nested location."""
    try:
        yield
    except InputError as error:
        if source and not error.location.startswith(source):
            raise type(error)(
                str(error), location=input_location(source, path)
            ) from error
        raise


def array_value(value: object, *, source: str = "") -> list[object]:
    """Require a JSON array without accepting arbitrary Python iterables."""
    if not isinstance(value, list):
        raise InputError("Expected a JSON array", location=source)
    return cast(list[object], value)


def string_value(value: object) -> str:
    """Require a string while leaving emptiness policy to its domain contract."""
    if not isinstance(value, str):
        raise InputError("Expected a string")
    return value


def optional_string_value(value: object) -> str | None:
    """Accept an unchecked optional string without coercing other JSON values."""
    return None if value is None else string_value(value)


def string_values(value: object) -> tuple[str, ...]:
    """Decode an array of strings into its immutable declaration representation."""
    return tuple(string_value(item) for item in array_value(value))


def boolean_value(value: object) -> bool:
    """Require an explicit JSON boolean rather than a truthy value."""
    if not isinstance(value, bool):
        raise InputError("Expected a boolean")
    return value
