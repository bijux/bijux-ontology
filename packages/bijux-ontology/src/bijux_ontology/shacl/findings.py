"""Owned SHACL result views and conservative joins to actual JSON-LD observations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.reports import Location
from bijux_ontology.shacl.paths import ShaclPath, decode_path

if TYPE_CHECKING:
    from bijux_ontology.jsonld.checking import PreparedUsages
    from bijux_ontology.jsonld.observations import ObservedOccurrence

_Edge = tuple[Node, Node, Node]


def term_data(node: Node | None) -> dict[str, object] | None:
    """Retain RDF term kind and lexical metadata within this report's scope."""
    if node is None:
        return None
    if isinstance(node, Literal):
        return {
            "kind": "literal",
            "value": str(node),
            "datatype": str(node.datatype) if node.datatype else None,
            "language": node.language,
        }
    return {"kind": "blank" if isinstance(node, BNode) else "iri", "value": str(node)}


@dataclass(frozen=True)
class ShaclSource:
    """Explicit prepared source selections eligible for semantic correspondence."""

    prepared: PreparedUsages
    paths: tuple[tuple[str | int, ...], ...]
    source: str = ""

    def __post_init__(self) -> None:
        # Optional JSON-LD dependencies are needed only for an actual source join.
        from bijux_ontology.jsonld.checking import PreparedUsages

        if not isinstance(self.prepared, PreparedUsages) or not isinstance(
            self.source, str
        ):
            raise InputError(
                "SHACL source correlation requires prepared JSON-LD usages"
            )
        if not isinstance(cast(object, self.paths), (tuple, list)) or any(
            not isinstance(cast(object, path), (tuple, list)) for path in self.paths
        ):
            raise InputError("SHACL source paths must be key/index sequences")
        paths = tuple(tuple(path) for path in self.paths)
        for path in paths:
            if not path:
                raise InputError("SHACL source selections require nonempty usage paths")
            Location(path=path)
        object.__setattr__(self, "paths", tuple(dict.fromkeys(paths)))


@dataclass(frozen=True)
class ShaclFinding:
    """Original RDF result fields with finite nested and source correspondence views."""

    focus_node: Node | None
    path: Node | None
    value: Node | None
    severity: Node | None
    source_shape: Node | None
    constraint_component: Node | None
    messages: tuple[Literal, ...]
    result_node: Node | None = None
    path_expression: ShaclPath | None = None
    details: tuple[ShaclFinding, ...] = ()
    detail_nodes: tuple[Node, ...] = ()
    details_complete: bool = True
    locations: tuple[Location, ...] = ()
    location_status: str = "unavailable"
    location_limitation: str | None = "No prepared source correspondence supplied"

    def to_dict(self) -> dict[str, object]:
        """Export typed fields and compact report-local detail references."""
        return {
            "result": term_data(self.result_node),
            "focus_node": term_data(self.focus_node),
            "path": self.path_expression.to_dict() if self.path_expression else None,
            "value": term_data(self.value),
            "severity": term_data(self.severity),
            "source_shape": term_data(self.source_shape),
            "constraint_component": term_data(self.constraint_component),
            "messages": [term_data(message) for message in self.messages],
            "details": [
                {"result": term_data(detail.result_node)} for detail in self.details
            ],
            "detail_nodes": [term_data(node) for node in self.detail_nodes],
            "details_complete": self.details_complete,
            "locations": [location.to_dict() for location in self.locations],
            "location_status": self.location_status,
            "location_limitation": self.location_limitation,
        }

    @property
    def evidence_json(self) -> str:
        """Return immutable canonical evidence suitable for generic issue transport."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _observed_edge(occurrence: ObservedOccurrence) -> _Edge | None:
    subject = occurrence.subject
    value = occurrence.value
    if (
        subject is None
        or subject.startswith("_:")
        or occurrence.container != "unordered"
    ):
        return None
    if occurrence.predicate == "@type" and isinstance(value, str):
        target: Node = URIRef(value)
        predicate = RDF.type
    elif isinstance(value, dict):
        raw = cast(dict[str, object], value)
        identifier = raw.get("@id")
        if isinstance(identifier, str) and not identifier.startswith("_:"):
            target = URIRef(identifier)
        elif isinstance(raw.get("@value"), str) and "@direction" not in raw:
            datatype, language = raw.get("@type"), raw.get("@language")
            target = Literal(
                raw["@value"],
                datatype=URIRef(datatype) if isinstance(datatype, str) else None,
                lang=language if isinstance(language, str) else None,
                normalize=False,
            )
        else:
            return None
        predicate = URIRef(occurrence.predicate)
    else:
        return None
    edge = (URIRef(subject), predicate, target)
    return (target, predicate, URIRef(subject)) if occurrence.reverse else edge


def _value_key(node: Node) -> Node | tuple[str, URIRef | None, str | None]:
    if isinstance(node, Literal):
        language = node.language.lower() if node.language else None
        return str(node), node.datatype or (None if language else XSD.string), language
    return node


def _edge_key(edge: _Edge) -> tuple[Node, Node, object]:
    return edge[0], edge[1], _value_key(edge[2])


def _present(data: Graph, edge: _Edge) -> bool:
    if edge in data:
        return True
    target = edge[2]
    if (
        isinstance(target, Literal)
        and not target.language
        and target.datatype in (None, XSD.string)
    ):
        alternate = Literal(
            str(target),
            datatype=None if target.datatype else XSD.string,
            normalize=False,
        )
        return (edge[0], edge[1], alternate) in data
    return False


class _SourceIndex:
    def __init__(
        self,
        sources: Sequence[ShaclSource],
        data: Graph,
        graph_name: str | None,
        union: bool,
        budget: Budget,
    ) -> None:
        self.edges: dict[tuple[Node, Node, object], list[Location]] = {}
        self.available = bool(sources)
        self.limited = union
        self.budget = budget
        for source in sources:
            for path in source.paths:
                budget.consume("work")
                usage = source.prepared.observe(path)
                self.limited |= usage.limitation is not None
                pointer = "/" + "/".join(
                    str(key).replace("~", "~0").replace("/", "~1") for key in path
                )
                for occurrence in usage.occurrences:
                    budget.consume("work")
                    if (
                        union
                        or occurrence.anonymous_graph
                        or occurrence.graph != graph_name
                    ):
                        continue
                    if occurrence.location != pointer:
                        self.limited = True
                        continue
                    edge = _observed_edge(occurrence)
                    if edge is None:
                        self.limited = True
                        continue
                    # Associate only facts present in this exact selected graph.
                    if _present(data, edge):
                        self.edges.setdefault(_edge_key(edge), []).append(
                            Location(source.source, path, graph=graph_name)
                        )

    def locate(
        self,
        data: Graph,
        focus: Node | None,
        path: ShaclPath | None,
        value: Node | None,
    ) -> tuple[tuple[Location, ...], str, str | None]:
        """Retain every established terminal occurrence and qualify ambiguity."""
        if not self.available:
            return (), "unavailable", "No prepared source correspondence supplied"
        if not isinstance(focus, URIRef) or path is None:
            return (
                (),
                "unverified",
                "Source association is ambiguous or outside the supported graph scope",
            )
        try:
            ends = _path_edges(data, focus, path, self.budget)
        except (InputError, LimitExceeded) as error:
            return (), "unverified", str(error)
        matched: list[Location] = []
        for end, edge in ends:
            self.budget.consume("work")
            if value is None or _value_key(end) == _value_key(value):
                matched.extend(self.edges.get(_edge_key(edge), ()))
        locations = tuple(dict.fromkeys(matched))
        if self.limited:
            return (
                locations,
                "unverified",
                "Some source associations are ambiguous or unsupported",
            )
        if not locations:
            return (
                (),
                "unavailable",
                "No established source occurrence matches this result",
            )
        return locations, "multiple" if len(locations) > 1 else "matched", None


def _path_edges(
    data: Graph,
    start: Node,
    path: ShaclPath,
    budget: Budget,
    inverse: bool = False,
) -> tuple[tuple[Node, _Edge], ...]:
    budget.consume("work")
    if path.kind == "predicate":
        edges = (
            (
                ((subject, path.node, start), subject)
                for subject in data.subjects(path.node, start)
            )
            if inverse
            else (
                ((start, path.node, target), target)
                for target in data.objects(start, path.node)
            )
        )
        result = []
        for edge, end in edges:
            budget.consume("states")
            if any(isinstance(node, BNode) for node in edge):
                raise InputError(
                    "Blank-node source identities have no cross-snapshot correspondence"
                )
            result.append((end, edge))
        return tuple(result)
    if path.kind == "inverse":
        return _path_edges(data, start, path.children[0], budget, not inverse)
    if path.kind == "alternative":
        return tuple(
            record
            for child in path.children
            for record in _path_edges(data, start, child, budget, inverse)
        )
    if path.kind == "sequence":
        current: tuple[tuple[Node, _Edge], ...] = ()
        nodes: tuple[Node, ...] = (start,)
        for child in reversed(path.children) if inverse else path.children:
            current = tuple(
                record
                for node in nodes
                for record in _path_edges(data, node, child, budget, inverse)
            )
            nodes = tuple(node for node, _ in current)
        return current
    raise InputError("Source correspondence for this path operator is unavailable")


def project_findings(
    graph: Graph,
    data: Graph,
    *,
    sources: Sequence[ShaclSource] = (),
    graph_name: str | None = None,
    union: bool = False,
    limits: Limits | None = None,
) -> tuple[tuple[ShaclFinding, ...], tuple[ShaclFinding, ...], bool]:
    """Project raw results without changing the graph, conformance or severities."""
    budget = Budget(limits)
    try:
        index = _SourceIndex(sources, data, graph_name, union, budget)
    except LimitExceeded:
        return (), (), False
    cache: dict[Node, ShaclFinding] = {}
    complete = True

    def walk(node: Node, active: frozenset[Node], depth: int) -> ShaclFinding:
        """Project report-local detail edges without expanding cycles or shared DAGs."""
        nonlocal complete
        if node in cache:
            return cache[node]
        budget.consume("states")
        detail_nodes = tuple(sorted(set(graph.objects(node, SH.detail)), key=str))
        bounded = node not in active and depth < min(budget.limits.max_depth, 128)
        complete &= bounded
        children = (
            tuple(walk(child, active | {node}, depth + 1) for child in detail_nodes)
            if bounded
            else ()
        )
        path_node = graph.value(node, SH.resultPath)
        path = (
            decode_path(graph, path_node, budget=budget)
            if path_node is not None
            else None
        )
        complete &= path is None or path.limitation is None
        focus, value = graph.value(node, SH.focusNode), graph.value(node, SH.value)
        locations, status, limitation = index.locate(data, focus, path, value)
        finding = ShaclFinding(
            focus,
            path_node,
            value,
            graph.value(node, SH.resultSeverity),
            graph.value(node, SH.sourceShape),
            graph.value(node, SH.sourceConstraintComponent),
            tuple(
                message
                for message in graph.objects(node, SH.resultMessage)
                if isinstance(message, Literal)
            ),
            node,
            path,
            children,
            detail_nodes,
            bounded and all(child.details_complete for child in children),
            locations,
            status,
            limitation,
        )
        if bounded:
            cache[node] = finding
        return finding

    nodes = set(graph.subjects(RDF.type, SH.ValidationResult)) | set(
        graph.objects(None, SH.detail)
    )
    roots = tuple(sorted(set(graph.objects(None, SH.result)), key=str))
    nodes.update(roots)
    try:
        for node in sorted(nodes, key=str):
            walk(node, frozenset(), 0)
    except LimitExceeded:
        complete = False
    return (
        tuple(cache[node] for node in sorted(nodes, key=str) if node in cache),
        tuple(cache[node] for node in roots if node in cache),
        complete,
    )
