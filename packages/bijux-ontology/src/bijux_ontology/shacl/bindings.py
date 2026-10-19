"""Snapshot-scoped RDF term selection and role-preserving worker transport."""

from collections.abc import Mapping, Sequence
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource

SELECTION = Namespace("urn:bijux:shacl:selection:")
TermSelection = str | URIRef | BNode | Literal


def resolve_terms(
    values: Sequence[TermSelection],
    source: Graph | PreparedSource,
    graph: Graph,
    *,
    shapes: bool = False,
    limits: Limits,
) -> tuple[URIRef | BNode | Literal, ...]:
    """Bind blanks by owned term objects, never by equal foreign labels."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InputError("RDF term selections require a finite sequence")
    if len(values) > limits.max_states:
        raise LimitExceeded("RDF term selections exceed max_states")
    owned = {id(node) for triple in graph for node in triple if isinstance(node, BNode)}
    result: list[URIRef | BNode | Literal] = []
    for value in values:
        if isinstance(value, BNode):
            if not isinstance(source, PreparedSource) or id(value) not in owned:
                raise InputError("Blank selection must come from its owned snapshot")
            node: URIRef | BNode | Literal = value
        elif isinstance(value, Literal):
            if shapes:
                raise InputError("An authored shape must be an IRI or owned blank")
            node = value
        elif isinstance(value, (str, URIRef)):
            node = URIRef(absolute_iri(str(value)))
        else:
            raise InputError("Expected an IRI, literal or owned blank RDF term")
        if node not in result:
            result.append(node)
    return tuple(result)


def transport_roles(
    graphs: Mapping[str, Graph],
    focus: Sequence[URIRef | BNode | Literal],
    shapes: Sequence[URIRef | BNode | Literal],
    limits: Limits,
) -> Dataset:
    """Standardize independent role blanks apart and carry exact bound endpoints.

    Authored and executable shapes share one scope. Exact shared term objects
    witness intentional sharing across roles; equal foreign labels do not.
    Bindings live in a separate
    transport graph and never become assertions in the effective data graph.
    """
    dataset = Dataset()
    budget = Budget(limits)
    parents: dict[tuple[str, BNode], tuple[str, BNode]] = {}
    witnesses: dict[int, tuple[str, BNode]] = {}
    blanks: dict[tuple[str, BNode], BNode] = {}

    def root(key: tuple[str, BNode]) -> tuple[str, BNode]:
        """Resolve finite role identities linked by an exact shared term object."""
        current = key
        while parents[current] != current:
            current = parents[current]
        while parents[key] != key:
            following = parents[key]
            parents[key] = current
            key = following
        return current

    for role, graph in graphs.items():
        scope = "shapes" if role == "authored-shapes" else role
        for triple in graph:
            for node in triple:
                if not isinstance(node, BNode):
                    continue
                key = (scope, node)
                if key not in parents:
                    budget.consume("states")
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
            blanks[key] = BNode()
        return blanks[key]

    for role, graph in graphs.items():
        destination = dataset.graph(URIRef(f"urn:bijux:shacl:{role}"))
        for subject, predicate, value in graph:
            budget.consume("work")
            destination.add((own(role, subject), predicate, own(role, value)))
    bindings = dataset.graph(URIRef("urn:bijux:shacl:bindings"))
    for predicate, nodes, role in (
        (SELECTION.focus, focus, "data"),
        (SELECTION.shape, shapes, "shapes"),
    ):
        for node in nodes:
            budget.consume("work")
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
