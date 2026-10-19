"""Snapshot-scoped RDF term selection and role-preserving worker transport."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.term import Node

from bijux_ontology._jsonschema import capture, operation
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node

SELECTION = Namespace("urn:bijux:shacl:selection:")
TermSelection = str | URIRef | BNode | Literal


def resolve_terms(
    values: Sequence[TermSelection],
    source: Graph | PreparedSource,
    graph: Graph,
    *,
    shapes: bool = False,
    limits: Limits,
    budget: Budget | None = None,
) -> tuple[URIRef | BNode | Literal, ...]:
    """Bind blanks by owned term objects, never by equal foreign labels."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InputError("RDF term selections require a finite sequence")
    if len(values) > limits.max_states:
        raise LimitExceeded("RDF term selections exceed max_states")
    budget = operation(limits, budget)
    selected = capture(values, budget)
    owned: set[int] = set()
    if any(isinstance(value, BNode) for value in selected):
        budget.consume("work", 3 * len(graph))
        budget.consume("states", 3 * len(graph))
        budget.consume("bytes", 64 + 128 * 3 * len(graph))
        owned = {
            id(node) for triple in graph for node in triple if isinstance(node, BNode)
        }
    result: list[URIRef | BNode | Literal] = []
    for value in selected:
        if isinstance(value, BNode):
            bound_node(value, budget)
            if not isinstance(source, PreparedSource) or id(value) not in owned:
                raise InputError("Blank selection must come from its owned snapshot")
            node: URIRef | BNode | Literal = value
        elif isinstance(value, Literal):
            bound_node(value, budget)
            if shapes:
                raise InputError("An authored shape must be an IRI or owned blank")
            node = value
        elif isinstance(value, str):
            bound_text(value, budget)
            budget.consume("bytes", 64 + 4 * len(value))
            node = URIRef(absolute_iri(str(value)))
        else:
            raise InputError("Expected an IRI, literal or owned blank RDF term")
        budget.consume("work", len(result) + 1)
        if node not in result:
            result.append(node)
    return tuple(result)


def transport_roles(
    graphs: Mapping[str, Graph],
    focus: Sequence[URIRef | BNode | Literal],
    shapes: Sequence[URIRef | BNode | Literal],
    limits: Limits,
    *,
    anchors: Mapping[URIRef, tuple[str, BNode]] | None = None,
    budget: Budget | None = None,
) -> Dataset:
    """Standardize independent role blanks apart and carry exact bound endpoints.

    Authored and executable shapes share one scope. Exact shared term objects
    witness intentional sharing across roles; equal foreign labels do not.
    Bindings live in a separate
    transport graph and never become assertions in the effective data graph.
    An explicit Budget also pays cumulative host storage and scalar preparation;
    independent callers retain their per-stage work and blank-node bounds.
    """
    shared = budget is not None
    budget = operation(limits, budget)

    def reserve(count: int = 1, size: int = 256) -> None:
        """Charge cumulative host ownership only for an explicit shared operation."""
        if shared:
            budget.consume("states", count)
            budget.consume("bytes", size)

    reserve(4, 512)
    dataset = Dataset()
    parents: dict[tuple[str, BNode], tuple[str, BNode]] = {}
    witnesses: dict[int, tuple[str, BNode]] = {}
    blanks: dict[tuple[str, BNode], BNode] = {}

    def root(key: tuple[str, BNode]) -> tuple[str, BNode]:
        """Resolve finite role identities linked by an exact shared term object."""
        current = key
        while parents[current] != current:
            if shared:
                budget.consume("work")
            current = parents[current]
        while parents[key] != key:
            if shared:
                budget.consume("work")
            following = parents[key]
            parents[key] = current
            key = following
        return current

    for role, graph in graphs.items():
        if shared:
            bound_text(role, budget)
        scope = "shapes" if role == "authored-shapes" else role
        for triple in graph:
            if shared:
                budget.consume("work", 3)
            for node in triple:
                if shared:
                    bound_node(node, budget)
                if not isinstance(node, BNode):
                    continue
                key = (scope, node)
                if key not in parents:
                    budget.consume("states")
                    reserve(2, 384)
                    parents[key] = key
                previous = witnesses.get(id(node))
                if previous is not None:
                    parents[root(key)] = root(previous)
                witnesses[id(node)] = key

    def own(role: str, node: Node) -> Node:
        """Retain named terms and map each role's blank identity exactly once."""
        if not isinstance(node, BNode):
            return node
        scope = "shapes" if role == "authored-shapes" else role
        key = root((scope, node))
        if key not in blanks:
            reserve(1, 128)
            blanks[key] = BNode()
        return blanks[key]

    for role, graph in graphs.items():
        if shared:
            bound_text(role, budget)
        reserve(1, 256 + 4 * len(role))
        destination = dataset.graph(URIRef(f"urn:bijux:shacl:{role}"))
        for subject, predicate, value in graph:
            budget.consume("work")
            reserve()
            destination.add((own(role, subject), predicate, own(role, value)))
    if anchors:
        reserve()
        anchored = dataset.graph(URIRef("urn:bijux:shacl:anchors"))
        for anchor, (role, node) in anchors.items():
            budget.consume("work")
            if shared:
                bound_text(role, budget)
            if shared:
                bound_node(anchor, budget)
                bound_node(node, budget)
            if role not in graphs or not isinstance(node, BNode):
                raise InputError("Static blank anchors require an owned role endpoint")
            reserve()
            anchored.add((anchor, ANCHOR["term"], own(role, node)))
    reserve()
    bindings = dataset.graph(URIRef("urn:bijux:shacl:bindings"))
    for predicate, nodes, role in (
        (SELECTION.focus, focus, "data"),
        (SELECTION.shape, shapes, "shapes"),
    ):
        for node in nodes:
            budget.consume("work")
            if shared:
                bound_node(node, budget)
            reserve()
            bindings.add((SELECTION.scope, predicate, own(role, node)))
    return dataset


def read_bindings(dataset: Dataset) -> tuple[tuple[Node, ...], tuple[Node, ...]]:
    """Resolve parsed anchors within the same dataset as their selected roles."""
    graph = dataset.graph(URIRef("urn:bijux:shacl:bindings"))
    return cast(
        tuple[tuple[Node, ...], tuple[Node, ...]],
        tuple(
            tuple(graph.objects(SELECTION.scope, predicate))
            for predicate in (SELECTION.focus, SELECTION.shape)
        ),
    )


ANCHOR = Namespace("urn:bijux:shacl:anchor:")


class StaticBindings:
    """Retain exact original blank witnesses while a fixed worker plan is alive."""

    def __init__(
        self,
        graphs: Mapping[str, Graph],
        limits: Limits,
        *,
        budget: Budget | None = None,
    ) -> None:
        budget = operation(limits, budget)
        budget.consume("states", 2)
        budget.consume("bytes", 256)
        self._nodes: dict[int, tuple[BNode, URIRef]] = {}
        self._limits = limits
        self._anchors: dict[URIRef, tuple[str, BNode]] = {}
        for role, graph in graphs.items():
            bound_text(role, budget)
            for triple in graph:
                budget.consume("work")
                for node in triple:
                    bound_node(node, budget)
                    if isinstance(node, BNode) and id(node) not in self._nodes:
                        budget.consume("states", 2)
                        budget.consume("bytes", 384)
                        anchor = URIRef(f"urn:bijux:shacl:anchor:{len(self._nodes)}")
                        self._nodes[id(node)] = (node, anchor)
                        self._anchors[anchor] = (role, node)

    @property
    def anchors(self) -> Mapping[URIRef, tuple[str, BNode]]:
        """Anchor each blank within its own role before emitting transport triples."""
        return MappingProxyType(self._anchors)

    def references(self, graph: Graph, *, budget: Budget | None = None) -> Graph:
        """Bind only exact shared objects, retaining witnesses against id reuse."""
        budget = operation(self._limits, budget)
        budget.consume("states")
        budget.consume("bytes", 256)
        result = Graph()
        for triple in graph:
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
                previous = self._nodes.get(id(node))
                if previous is not None and previous[0] is node:
                    budget.consume("states")
                    budget.consume("bytes", 256)
                    result.add((previous[1], ANCHOR["term"], node))
        return result
