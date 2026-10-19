"""Owned JSON Patch operations executed by the maintained RFC implementation."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from importlib.metadata import version
from typing import Any

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json import snapshots
from bijux_ontology.jsonld.contexts.preparation import (
    context_data,
    preflight_context_json,
)
from bijux_ontology.jsonld.processing.resources import (
    ContextResources,
    DocumentLoader,
    context_resources,
)


class _Unsupported(InputError):
    """The requested edit or association exceeds the finite checked profile."""


class _Precondition(InputError):
    """The supplied exact base or maintained JSON Patch test did not hold."""


def declarations(
    values: Sequence[Any], kind: type[Any], budget: Budget
) -> tuple[Any, ...]:
    """Capture typed requirements before a loader can mutate caller metadata."""
    result = []
    for value in capture(values, budget):
        if not isinstance(value, kind):
            raise InputError("Source patches require typed form/usage declarations")
        context_data(value, budget)
        result.append(replace(value))
    return tuple(result)


def loader_resources(loader: DocumentLoader | None, budget: Budget) -> ContextResources:
    """Capture each actual response once, with no implicit retrieval channel."""
    if loader is None or isinstance(loader, ContextResources):
        return context_resources(loader, budget=budget)

    def qualified(url: str, options: dict[str, object]) -> dict[str, object]:
        """Bound returned metadata before ordinary shared resource ownership."""
        try:
            response = loader(url, options)
        except InputError as error:
            bound_text(str(error), budget)
            raise
        preflight_context_json(response, budget)
        return response

    return context_resources(qualified, budget=budget)


def prepare_operations(
    values: Sequence[object], budget: Budget
) -> tuple[dict[str, object], ...]:
    """Own only the explicitly supported RFC operations, without parsing pointers."""
    result = []
    for value in capture(values, budget):
        owned, _ = snapshots.snapshot(value, budget)
        if not isinstance(owned, dict):
            raise InputError("JSON Patch operations require objects")
        op = owned.get("op")
        if not isinstance(op, str) or not isinstance(owned.get("path"), str):
            raise InputError("JSON Patch operations require op and path strings")
        if op not in ("add", "remove", "replace", "test"):
            raise _Unsupported("Only add, remove, replace and test are qualified")
        required = {"op", "path"} if op == "remove" else {"op", "path", "value"}
        if set(owned) != required:
            raise InputError("JSON Patch operation fields must match the selected form")
        result.append(owned)
    if not result or all(item["op"] == "test" for item in result):
        raise _Unsupported("Source patches need an explicit modifying operation")
    return tuple(result)


def apply_operations(
    document: object, operations: tuple[dict[str, object], ...], budget: Budget
) -> Iterator[object]:
    """Run actual maintained pointer and RFC test semantics on a private tree."""
    try:
        import jsonpatch  # type: ignore[import-untyped]
        from jsonpointer import JsonPointerException  # type: ignore[import-untyped]
    except ImportError as error:
        raise ResourceError(
            "Source patches require bijux-ontology[jsonpatch]"
        ) from error
    installed = version("jsonpatch")
    bound_text(installed, budget)
    parts = installed.split(".")
    if (
        len(parts) not in (2, 3)
        or not all(part.isascii() and part.isdigit() for part in parts)
        or len(parts[0]) > 2
        or len(parts[1]) > 2
        or int(parts[0]) != 1
        or int(parts[1]) < 35
    ):
        raise ResourceError(
            "Source patches require the strict RFC tests in jsonpatch>=1.35"
        )
    current = document
    for operation in operations:
        before = snapshots.serialized(current, budget)
        payload = snapshots.serialized(operation, budget)
        budget.consume("work", len(before) + len(payload) + 1)
        # The backend owns pointer grammar, list insertion/removal and typed test
        # equality. Each operation has independent captured values; copy/move are
        # excluded so retained value growth is bounded by supplied edit payloads.
        try:
            current = jsonpatch.JsonPatch([operation]).apply(current, in_place=True)
        except jsonpatch.JsonPatchTestFailed as error:
            raise _Precondition("A supplied JSON Patch test failed") from error
        except (jsonpatch.JsonPatchException, JsonPointerException) as error:
            raise InputError("The supplied JSON Patch cannot be applied") from error
        snapshots.serialized(current, budget)
        yield current
