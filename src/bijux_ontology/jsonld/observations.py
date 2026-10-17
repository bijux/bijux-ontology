"""Immutable expanded usage evidence and its semantic contribution identities."""

import json
from dataclasses import dataclass
from typing import cast

from .values import ValueTraits, value_traits

SourcePath = tuple[str | int, ...]


def location(path: SourcePath) -> str:
    """Render the selected input occurrence as an escaped JSON Pointer."""
    return "/" + "/".join(
        str(key).replace("~", "~0").replace("/", "~1") for key in path
    )


def at_path(document: object, path: SourcePath) -> object:
    """Follow concrete keys and nonnegative array indexes."""
    current = document
    for key in path:
        if isinstance(key, str) and isinstance(current, dict):
            current = cast(dict[str, object], current)[key]
        elif type(key) is int and key >= 0 and isinstance(current, list):
            current = cast(list[object], current)[key]
        else:
            raise KeyError(key)
    return current


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ObservedOccurrence:
    """One observed edge value with its actual node, graph and collection scope.

    Value bytes are immutable. The value property returns an independent JSON
    copy. Location identifies the established selection, never a guessed parser
    offset or a fabricated source location for a processor callback.
    """

    predicate: str
    value_json: str
    subject: str | None = None
    graph: str | None = None
    reverse: bool = False
    container: str = "unordered"
    list_position: int | None = None
    depth: int = 0
    location: str = ""
    empty: bool = False

    @property
    def value(self) -> object:
        """Return the original expanded value independently of cached state."""
        return cast(object, json.loads(self.value_json))

    @property
    def traits(self) -> ValueTraits:
        """Inspect this occurrence without re-expansion or declaration inference."""
        return value_traits(self.value, self.predicate)

    @property
    def identity(self) -> tuple[object, ...]:
        """Identify semantic contribution independently of expansion positions."""
        value = self.value
        if isinstance(value, dict) and "@value" not in value:
            # Ordinary child properties do not change the containing edge.
            # Their contributions are separate occurrences below this node.
            value = {
                key: child
                for key, child in value.items()
                if key in {"@id", "@type", "@graph"}
            }
            if "@graph" in value:
                value["@graph"] = True
        elif isinstance(value, dict):
            value.pop("@index", None)
        return (
            self.predicate,
            _json(value),
            self.subject,
            self.graph,
            self.reverse,
            self.container,
            self.depth,
            self.empty,
        )


@dataclass(frozen=True)
class ObservedUsage:
    """Complete selected occurrences or an explicit association limitation."""

    path: SourcePath
    occurrences: tuple[ObservedOccurrence, ...] = ()
    limitation: str | None = None
    dropped: bool = False
    empty: bool = False


def _nodes(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else [value]


def occurrences(expanded: object) -> tuple[ObservedOccurrence, ...]:
    """Collect expanded contributions while assigning reversal to each edge."""
    result: list[ObservedOccurrence] = []

    def walk(value: object, graph: str | None, depth: int) -> None:
        """Retain node and graph scope without inheriting an ancestor's reversal."""
        if isinstance(value, list):
            for node in cast(list[object], value):
                walk(node, graph, depth)
            return
        if not isinstance(value, dict):
            return
        node = cast(dict[str, object], value)
        if "@value" in node:
            return
        subject = node.get("@id")
        subject = subject if isinstance(subject, str) else None
        if subject is not None:
            result.append(
                ObservedOccurrence("@id", _json(subject), None, graph, depth=depth)
            )
        for node_type in _nodes(node.get("@type", [])):
            result.append(
                ObservedOccurrence(
                    "@type", _json(node_type), subject, graph, depth=depth
                )
            )
        if "@graph" in node:
            walk(node["@graph"], subject or graph, depth)
        if "@included" in node:
            walk(node["@included"], graph, depth)
        for predicate, values in node.items():
            if not predicate.startswith("@"):
                edge(predicate, values, subject, graph, depth, False)
        reverse = node.get("@reverse")
        if isinstance(reverse, dict):
            for predicate, values in cast(dict[str, object], reverse).items():
                edge(predicate, values, subject, graph, depth, True)

    def edge(
        predicate: str,
        values: object,
        subject: str | None,
        graph: str | None,
        depth: int,
        reverse: bool,
    ) -> None:
        """Emit every member while preserving list sequence separately."""
        for value in _nodes(values):
            listed = isinstance(value, dict) and "@list" in value
            members = (
                _nodes(cast(dict[str, object], value)["@list"]) if listed else [value]
            )
            if listed and not members:
                result.append(
                    ObservedOccurrence(
                        predicate,
                        "[]",
                        subject,
                        graph,
                        reverse,
                        "list",
                        depth=depth,
                        empty=True,
                    )
                )
            for index, member in enumerate(members):
                result.append(
                    ObservedOccurrence(
                        predicate,
                        _json(member),
                        subject,
                        graph,
                        reverse,
                        "list" if listed else "unordered",
                        index if listed else None,
                        depth,
                    )
                )
                walk(member, graph, depth + 1)

    walk(expanded, None, 0)
    return tuple(result)
