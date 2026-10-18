"""Immutable expanded usage evidence and its semantic contribution identities."""

import json
from dataclasses import dataclass
from typing import cast

from bijux_ontology.limits import Budget

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
    offset or a fabricated source location for a processor callback. List IDs
    group members within one expansion only; they are not RDF identifiers or
    stable correspondence keys across independent expansions. anonymous_graph
    distinguishes an unnamed graph scope from the default graph without
    fabricating a blank-node identifier. Identity tokens associate implicit nodes
    across selected occurrences from one expansion, never across documents or
    independent expansions. Differential matching excludes these local tokens.
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
    list_id: int | None = None
    list_length: int | None = None
    anonymous_graph: bool = False
    subject_identity: int | None = None
    graph_identity: int | None = None
    identifier_identity: int | None = None

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
            self.anonymous_graph,
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


def occurrences(
    expanded: object, *, budget: Budget | None = None
) -> tuple[ObservedOccurrence, ...]:
    """Collect expanded contributions while assigning reversal to each edge."""
    result: list[ObservedOccurrence] = []
    list_count = 0
    identities: dict[int, int] = {}

    def retain(item: ObservedOccurrence) -> None:
        """Bound retained observations before allocation enters the inventory."""
        if budget is not None:
            budget.consume("work")
            budget.consume("states")
            budget.consume("bytes", len(item.value_json.encode("utf-8")))
        result.append(item)

    def identity(node: object) -> int:
        """Name an implicit node only within this owned expansion."""
        key = id(node)
        if key not in identities:
            if budget is not None:
                budget.consume("states")
            identities[key] = len(identities)
        return identities[key]

    def walk(
        value: object,
        graph: str | None,
        depth: int,
        anonymous_graph: bool = False,
        graph_identity: int | None = None,
    ) -> None:
        """Retain node and graph scope without inheriting an ancestor's reversal."""
        if isinstance(value, list):
            for node in cast(list[object], value):
                walk(node, graph, depth, anonymous_graph, graph_identity)
            return
        if not isinstance(value, dict):
            return
        node = cast(dict[str, object], value)
        if "@value" in node:
            return
        subject = node.get("@id")
        subject = subject if isinstance(subject, str) else None
        subject_identity = identity(node) if subject is None else None
        if subject is not None:
            retain(
                ObservedOccurrence(
                    "@id",
                    _json(subject),
                    None,
                    graph,
                    depth=depth,
                    anonymous_graph=anonymous_graph,
                    graph_identity=graph_identity,
                )
            )
        for node_type in _nodes(node.get("@type", [])):
            retain(
                ObservedOccurrence(
                    "@type",
                    _json(node_type),
                    subject,
                    graph,
                    depth=depth,
                    anonymous_graph=anonymous_graph,
                    subject_identity=subject_identity,
                    graph_identity=graph_identity,
                )
            )
        if "@graph" in node:
            # Public expansion removes the top-level default-graph wrapper.
            # Each surviving @graph establishes its own named scope, even when
            # its blank graph name was not explicitly supplied in the document.
            walk(node["@graph"], subject, depth, subject is None, subject_identity)
        if "@included" in node:
            walk(node["@included"], graph, depth, anonymous_graph, graph_identity)
        for predicate, values in node.items():
            if not predicate.startswith("@"):
                edge(
                    predicate,
                    values,
                    subject,
                    graph,
                    depth,
                    False,
                    anonymous_graph,
                    subject_identity,
                    graph_identity,
                )
        reverse = node.get("@reverse")
        if isinstance(reverse, dict):
            for predicate, values in cast(dict[str, object], reverse).items():
                edge(
                    predicate,
                    values,
                    subject,
                    graph,
                    depth,
                    True,
                    anonymous_graph,
                    subject_identity,
                    graph_identity,
                )

    def edge(
        predicate: str,
        values: object,
        subject: str | None,
        graph: str | None,
        depth: int,
        reverse: bool,
        anonymous_graph: bool,
        subject_identity: int | None,
        graph_identity: int | None,
    ) -> None:
        """Emit every member while preserving list sequence separately."""
        nonlocal list_count
        for value in _nodes(values):
            listed = isinstance(value, dict) and "@list" in value
            members = (
                _nodes(cast(dict[str, object], value)["@list"]) if listed else [value]
            )
            list_id = list_count if listed else None
            if listed:
                list_count += 1
            if listed and not members:
                retain(
                    ObservedOccurrence(
                        predicate,
                        "[]",
                        subject,
                        graph,
                        reverse,
                        "list",
                        depth=depth,
                        empty=True,
                        list_id=list_id,
                        list_length=0,
                        anonymous_graph=anonymous_graph,
                        subject_identity=subject_identity,
                        graph_identity=graph_identity,
                    )
                )
            for index, member in enumerate(members):
                retain(
                    ObservedOccurrence(
                        predicate,
                        _json(member),
                        subject,
                        graph,
                        reverse,
                        "list" if listed else "unordered",
                        index if listed else None,
                        depth,
                        list_id=list_id,
                        list_length=len(members) if listed else None,
                        anonymous_graph=anonymous_graph,
                        subject_identity=subject_identity,
                        graph_identity=graph_identity,
                        identifier_identity=(
                            identity(member)
                            if isinstance(member, dict)
                            and "@id" not in member
                            and "@value" not in member
                            else None
                        ),
                    )
                )
                walk(member, graph, depth + 1, anonymous_graph, graph_identity)

    walk(expanded, None, 0)
    return tuple(result)
