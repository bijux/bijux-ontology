"""Strict JSON input boundaries and independently owned caller values."""

import json
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import NoReturn, cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Limits


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
) -> object:
    """Validate JSON size, shape and cycles before making an independent copy.

    Parsed mappings cannot reveal duplicate names discarded by another decoder.
    Size is the compact UTF-8 JSON representation, not Python object overhead.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise InputError("max_bytes must be a positive integer")
    if type(max_depth) is not int or max_depth <= 0:
        raise InputError("max_depth must be a positive integer")
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    size = 0
    try:
        while stack:
            item, depth, leaving = stack.pop()
            if leaving:
                active.remove(id(item))
                continue
            if depth > max_depth:
                raise LimitExceeded("JSON nesting exceeds max_depth", location=source)
            if isinstance(item, (dict, list)):
                if id(item) in active:
                    raise InputError("Cyclic containers are not JSON", location=source)
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
                size += _string_bytes(item)
            elif item is None or type(item) in (bool, int, float):
                if isinstance(item, float) and not math.isfinite(item):
                    raise InputError("JSON numbers must be finite", location=source)
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


def decode_json(
    payload: str | bytes,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
) -> object:
    """Decode owned JSON text, rejecting ambiguous names at every object depth."""
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

    def objects(value: object, path: str, depth: int) -> object:
        """Resolve retained object pairs with an established JSON Pointer path."""
        if depth > max_depth:
            raise LimitExceeded(
                "JSON nesting exceeds max_depth", location=source + path
            )
        if isinstance(value, _ObjectPairs):
            result: dict[str, object] = {}
            for key, child in value:
                pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key in result:
                    cause = ValueError(f"Duplicate JSON object name: {key}")
                    raise InputError(str(cause), location=source + pointer) from cause
                result[key] = objects(child, pointer, depth + 1)
            return result
        if isinstance(value, list):
            return [
                objects(child, f"{path}/{index}", depth + 1)
                for index, child in enumerate(value)
            ]
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
