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

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Limits

if TYPE_CHECKING:
    from bijux_ontology.execution.limits import Budget


def _string_bytes(
    value: str, budget: Budget | None = None, workspace: list[int] | None = None
) -> int:
    """Measure maintained JSON escaping with one reusable bounded scratch allowance."""
    if budget is not None:
        budget.consume("work", len(value) + 1)
    # These scalars have no JSON escapes or UTF-8 expansion. Measuring their
    # exact spelling needs no temporary encoded copy or scratch reservation.
    if (
        value.isascii()
        and value.isprintable()
        and '"' not in value
        and "\\" not in value
    ):
        return len(value) + 2
    size = 2
    chunk_size = 32 if budget is not None else 4096
    for start in range(0, len(value), chunk_size):
        length = min(chunk_size, len(value) - start)
        if budget is not None:
            assert workspace is not None
            needed = 128 + 40 * length
            if needed > workspace[0]:
                budget.reserve_storage(needed - workspace[0])
                workspace[0] = needed
        encoded = json.dumps(
            value[start : start + chunk_size], ensure_ascii=False
        ).encode("utf-8")
        payload = len(encoded) - 2
        size += payload
    return size


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
    String subclasses use their exact JSON wire strings; name collisions are rejected.
    An optional enclosing budget also prepays traversal, container copies and
    scalar serialization workspace before allocating them. Its limits remain
    independent of any narrower explicit input-size/depth policy.
    """
    return capture_json(
        value, source=source, max_bytes=max_bytes, max_depth=max_depth, budget=budget
    ).value


@dataclass(frozen=True)
class JsonMeasurement:
    """Qualified wire size and traversal weight for the value at measurement time.

    Facts describe the inspected value; they are not a reusable authorization to
    skip validation after mutation. No API accepts a receipt instead of a value.
    Size excludes ordinary encoder separator spaces. Weight counts nodes,
    key/string lengths and numeric magnitude estimates for workspace bounds.
    """

    size: int
    ascii_only: bool
    separating_spaces: int
    weight: int


@dataclass(frozen=True)
class JsonCapture:
    """One independent JSON value and the facts established during its capture."""

    value: object
    measurement: JsonMeasurement


def capture_json(
    value: object,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    budget: Budget | None = None,
) -> JsonCapture:
    """Validate and measure once, then defensively own mutable caller containers."""
    measurement, memo = _inspect_json(
        value,
        source=source,
        max_bytes=max_bytes,
        max_depth=max_depth,
        budget=budget,
        copy_workspace=True,
    )
    try:
        return JsonCapture(deepcopy(value, memo), measurement)
    except RecursionError as error:
        raise LimitExceeded(
            "JSON copy exceeds the Python runtime nesting limit", location=source
        ) from error


def measure_json(
    value: object,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    budget: Budget | None = None,
) -> JsonMeasurement:
    """Validate current JSON and return facts without retaining a copy of the value."""
    return _inspect_json(
        value, source=source, max_bytes=max_bytes, max_depth=max_depth, budget=budget
    )[0]


def encode_json(value: object, *, budget: Budget) -> str:
    """Prepay maintained JSON encoding with the ordinary unescaped output format."""
    from bijux_ontology.execution.limits import Budget as RuntimeBudget

    if not isinstance(budget, RuntimeBudget):
        raise InputError("JSON encoding requires a Budget")
    measurement = measure_json(
        value,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
        budget=budget,
    )
    size = measurement.size + measurement.separating_spaces
    if size > budget.limits.max_bytes:
        raise LimitExceeded("JSON output exceeds max_bytes")
    budget.consume("work", size + 1)
    # Reserve fixed encoder scratch, intermediate text and the retained result.
    # Non-ASCII Python strings can use four-byte characters independently of UTF-8.
    budget.reserve_storage(1024 + (4 if measurement.ascii_only else 8) * size)
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _inspect_json(
    value: object,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    budget: Budget | None = None,
    copy_workspace: bool = False,
) -> tuple[JsonMeasurement, dict[int, object] | None]:
    """Validate original JSON and measure bytes without making a redundant snapshot."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise InputError("max_bytes must be a positive integer")
    if type(max_depth) is not int or max_depth <= 0:
        raise InputError("max_depth must be a positive integer")
    if budget is not None:
        from bijux_ontology.execution.limits import Budget as RuntimeBudget

        if not isinstance(budget, RuntimeBudget):
            raise InputError("JSON ownership requires a Budget")
        max_bytes = min(max_bytes, budget.limits.max_bytes)
        max_depth = min(max_depth, budget.limits.max_depth)
        budget.reserve_storage(128)

    workspace = [0]

    stack: list[tuple[Iterator[object], int, int | None]] = [(iter((value,)), 0, None)]
    active: set[int] = set()
    size = 0
    weight = 0
    ascii_only = True
    separating_spaces = 0
    string_memo: dict[int, object] | None = None

    def canonical_string(value: str) -> str:
        """Prepay the actual JSON spelling without caller string conversion hooks."""
        nonlocal string_memo
        if budget is not None:
            budget.consume("work", len(value) + 1)
            budget.reserve_storage(256 + 4 * len(value))
        name = str.__str__(value)
        if copy_workspace:
            if string_memo is None:
                string_memo = {}
            string_memo[id(value)] = name
        return name

    try:
        while stack:
            if budget is not None:
                budget.consume("work")
            children, depth, owner = stack[-1]
            try:
                item = next(children)
            except StopIteration:
                stack.pop()
                if owner is not None:
                    active.remove(owner)
                continue
            if budget is not None:
                budget.consume("states")
            weight += 1
            if depth > max_depth:
                raise LimitExceeded("JSON nesting exceeds max_depth", location=source)
            if isinstance(item, (dict, list)):
                if id(item) in active:
                    raise InputError("Cyclic containers are not JSON", location=source)
                if budget is not None:
                    budget.reserve_storage(
                        (
                            (256 + 48 * len(item))
                            if isinstance(item, dict)
                            else (128 + 16 * len(item))
                        )
                        if copy_workspace
                        else 128,
                    )
                active.add(id(item))
                size += 2 + max(0, len(item) - 1)
                separating_spaces += max(0, len(item) - 1)
                if size > max_bytes:
                    raise LimitExceeded("JSON input exceeds max_bytes", location=source)
                if isinstance(item, dict):
                    separating_spaces += len(item)
                    subclass_names = False
                    for key in item:
                        if not isinstance(key, str):
                            raise InputError(
                                "JSON object names must be strings", location=source
                            )
                        if len(key) > max_bytes:
                            raise LimitExceeded(
                                "JSON input exceeds max_bytes", location=source
                            )
                        subclass_names |= type(key) is not str
                        weight += len(key)
                        ascii_only &= key.isascii()
                        size += _string_bytes(key, budget, workspace) + 1
                        if size > max_bytes:
                            raise LimitExceeded(
                                "JSON input exceeds max_bytes", location=source
                            )
                    if subclass_names:
                        # Python key equality may differ from the JSON string name.
                        # Only subclass-bearing objects need another name inventory.
                        if budget is not None:
                            budget.consume("work", len(item))
                            budget.consume("states", len(item))
                            budget.reserve_storage(256 + 80 * len(item))
                        names: set[str] = set()
                        for key in item:
                            name = key if type(key) is str else canonical_string(key)
                            if name in names:
                                raise InputError(
                                    "JSON object names collide on the wire",
                                    location=source,
                                )
                            names.add(name)
                    stack.append((iter(item.values()), depth + 1, id(item)))
                else:
                    stack.append((iter(item), depth + 1, id(item)))
            elif isinstance(item, str):
                if len(item) > max_bytes:
                    raise LimitExceeded("JSON input exceeds max_bytes", location=source)
                weight += len(item)
                ascii_only &= item.isascii()
                size += _string_bytes(item, budget, workspace)
                if copy_workspace and type(item) is not str:
                    canonical_string(item)
            elif item is None or type(item) is bool:
                length = 4 if item is None or item is True else 5
                size += length
            elif type(item) in (int, float):
                if isinstance(item, float) and not math.isfinite(item):
                    raise InputError("JSON numbers must be finite", location=source)
                if budget is not None:
                    digits = (
                        max(1, (item.bit_length() * 30103) // 100000 + 1)
                        if type(item) is int
                        else 32
                    )
                    budget.consume("work", digits)
                    budget.reserve_storage(64 + 16 * digits)
                spelling = json.dumps(item)
                size += len(spelling)
                weight += (
                    max(1, (item.bit_length() * 30103) // 100000 + 1)
                    if type(item) is int
                    else len(spelling)
                )
            else:
                raise InputError("Expected only JSON values", location=source)
            if size > max_bytes:
                raise LimitExceeded("JSON input exceeds max_bytes", location=source)
    except (UnicodeError, ValueError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError(str(error), location=source) from error
    return JsonMeasurement(size, ascii_only, separating_spaces, weight), string_memo


def owned_json(value: object, budget: Budget) -> tuple[object, int]:
    """Return an independently owned snapshot and its single-pass traversal weight."""
    captured = capture_json(
        value,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
        budget=budget,
    )
    return captured.value, captured.measurement.weight


class _ObjectPairs(list[tuple[str, object]]):
    """Keep duplicate names until their actual object path is known."""


def _invalid_constant(value: str) -> NoReturn:
    raise InputError(f"Non-finite number is not JSON: {value}")


@dataclass(frozen=True)
class _RawNumber:
    """Retain a maintained decoder's validated numeric token until path resolution."""

    token: str
    integer: bool


JsonNumberHook = Callable[[str, tuple[str | int, ...], bool], object]


def decode_json(
    payload: str | bytes,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    budget: Budget | None = None,
) -> object:
    """Decode owned JSON text, rejecting ambiguous names at every object depth."""
    return decode_owned_json(
        payload, source=source, max_bytes=max_bytes, max_depth=max_depth, budget=budget
    ).value


def decode_owned_json(
    payload: str | bytes,
    *,
    source: str = "",
    max_bytes: int = 5_000_000,
    max_depth: int = 128,
    number_hook: JsonNumberHook | None = None,
    key_hook: Callable[[str], None] | None = None,
    budget: Budget | None = None,
) -> JsonCapture:
    """Decode and qualify one owned tree with optional source-token observations.

    Maintained JSON parsing and duplicate resolution own their workspace here.
    Plain parsed containers are already private and need no second deep copy.
    Numeric hooks may return caller aliases, so that profile receives one final
    defensive capture. Hooks own their scalar policy; returned values must still
    be finite JSON. A receipt never authorizes later reuse after value mutation.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise InputError("max_bytes must be a positive integer")
    if type(max_depth) is not int or max_depth <= 0:
        raise InputError("max_depth must be a positive integer")
    if budget is not None:
        from bijux_ontology.execution.limits import Budget as RuntimeBudget

        if not isinstance(budget, RuntimeBudget):
            raise InputError("JSON decoding requires a Budget")
        max_bytes = min(max_bytes, budget.limits.max_bytes)
        max_depth = min(max_depth, budget.limits.max_depth)
    if not isinstance(payload, (str, bytes)):
        raise InputError("Expected JSON text or bytes", location=source)
    # UTF-8 cannot be shorter than the original Unicode character inventory.
    if len(payload) > max_bytes:
        raise LimitExceeded("JSON input exceeds max_bytes", location=source)
    try:
        if budget is not None and isinstance(payload, str):
            budget.consume("work", len(payload) + 1)
            budget.reserve_storage(128 + 4 * len(payload))
        size = (
            len(payload) if isinstance(payload, bytes) else len(payload.encode("utf-8"))
        )
        if size > max_bytes:
            raise LimitExceeded("JSON input exceeds max_bytes", location=source)
        if budget is not None:
            # Pairs and resolved containers coexist during native decoding.
            # Reserve their bounded inventory before the parser allocates it.
            budget.reserve_storage(128 + 96 * size)
            budget.consume("work", size + 1)
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

    def child_parts(
        parts: tuple[str | int, ...], child: str | int
    ) -> tuple[str | int, ...]:
        """Reserve path storage without repeatedly copying escaped source strings."""
        if budget is not None:
            budget.reserve_storage(48 + 8 * (len(parts) + 1))
        return (*parts, child)

    def pointer(parts: tuple[str | int, ...]) -> str:
        """Format the real JSON pointer only when a failing location is needed."""
        size = 1
        for part in parts:
            text = str(part)
            size += len(text.encode("utf-8")) + text.count("~") + text.count("/") + 1
            if size > max_bytes:
                raise LimitExceeded("JSON location exceeds max_bytes", location=source)
        if budget is not None:
            budget.consume("work", size)
            budget.reserve_storage(128 + 8 * size)
        return "/" + "/".join(
            str(part).replace("~", "~0").replace("/", "~1") for part in parts
        )

    def objects(value: object, depth: int, parts: tuple[str | int, ...]) -> object:
        """Resolve native object pairs and retain exact hook source positions."""
        if budget is not None:
            budget.consume("work")
            budget.consume("states")
        if depth > max_depth:
            raise LimitExceeded(
                "JSON nesting exceeds max_depth", location=source + pointer(parts)
            )
        if isinstance(value, _ObjectPairs):
            result: dict[str, object] = {}
            for key, child in value:
                if key_hook is not None:
                    key_hook(key)
                selected = child_parts(parts, key)
                if key in result:
                    cause = ValueError(f"Duplicate JSON object name: {key}")
                    raise InputError(
                        str(cause), location=source + pointer(selected)
                    ) from cause
                result[key] = objects(child, depth + 1, selected)
            return result
        if isinstance(value, list):
            return [
                objects(child, depth + 1, child_parts(parts, index))
                for index, child in enumerate(value)
            ]
        if isinstance(value, _RawNumber):
            assert number_hook is not None
            return number_hook(value.token, parts, value.integer)
        return value

    try:
        resolved = objects(data, 0, ())
    except RecursionError as error:
        raise LimitExceeded(
            "JSON nesting exceeds decoder limit", location=source
        ) from error
    if number_hook is not None:
        return capture_json(
            resolved,
            source=source,
            max_bytes=max_bytes,
            max_depth=max_depth,
            budget=budget,
        )
    return JsonCapture(
        resolved,
        measure_json(
            resolved,
            source=source,
            max_bytes=max_bytes,
            max_depth=max_depth,
            budget=budget,
        ),
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
                str(error),
                location=input_location(source, path),
                failure=error.failure,
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
