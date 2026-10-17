"""Independent graph and dataset snapshots with explicit scope and fidelity."""

from typing import Literal as TypingLiteral
from typing import cast

from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, URIRef
from rdflib.term import Node

from ..errors import InputError, LimitExceeded
from .identifiers import absolute_iri
from .terms import validate_node, validate_triple

GraphSelection = URIRef | BNode | str


def bind_namespaces(target: Graph, *sources: Graph) -> None:
    """Retain readable bindings without confusing prefix labels with RDF identity."""
    bindings: dict[str, set[str]] = {}
    for source in sources:
        for prefix, namespace in source.namespaces():
            bindings.setdefault(prefix, set()).add(str(namespace))
    used: set[str] = set(bindings)
    for prefix, namespaces in sorted(bindings.items()):
        for index, namespace_iri in enumerate(sorted(namespaces)):
            label = prefix
            if index:
                suffix = index
                while f"{prefix}_{suffix}" in used:
                    suffix += 1
                label = f"{prefix}_{suffix}"
            used.add(label)
            target.bind(label, URIRef(namespace_iri), override=True, replace=True)


def copy_graph(
    source: Graph, *, max_triples: int = 1_000_000, standardize_apart: bool = True
) -> Graph:
    """Own a validated graph, separating document blank nodes by default.

    Deliberately shared caller graph roles may retain immutable blank-node IDs
    with ``standardize_apart=False``; separate parsed documents must stay apart.
    """
    if type(standardize_apart) is not bool:
        raise InputError("standardize_apart must be a boolean")
    if isinstance(source, Dataset | ConjunctiveGraph):
        raise InputError("Dataset sources require explicit graph selection or union")
    if len(source) > max_triples:
        raise LimitExceeded("RDF graph exceeds max_triples")
    graph = Graph(bind_namespaces="none")
    nodes: dict[BNode, BNode] = {}

    def own(node: Node) -> Node:
        """Keep this document's blank-node identity separate from other sources."""
        return (
            nodes.setdefault(node, BNode())
            if standardize_apart and isinstance(node, BNode)
            else node
        )

    for subject, predicate, value in source:
        validate_triple(subject, predicate, value)
        graph.add((own(subject), own(predicate), own(value)))
    bind_namespaces(graph, source)
    return graph


def copy_dataset(
    source: Dataset | ConjunctiveGraph,
    max_triples: int,
    *,
    standardize_apart: bool = True,
) -> Dataset:
    """Own graph scopes and terms while retaining cross-graph blank-node links."""
    if len(source) > max_triples:
        raise LimitExceeded("RDF dataset exceeds max_triples")
    dataset = Dataset()
    nodes: dict[BNode, BNode] = {}

    def own(node: Node) -> Node:
        """Share one blank-node mapping across all graphs in this dataset."""
        return (
            nodes.setdefault(node, BNode())
            if standardize_apart and isinstance(node, BNode)
            else node
        )

    default = (
        source.default_graph.identifier
        if isinstance(source, Dataset)
        else source.default_context.identifier
    )
    contexts = source.graphs() if isinstance(source, Dataset) else source.contexts()
    for context in contexts:
        if not isinstance(context.identifier, URIRef | BNode):
            raise InputError("RDF graph names must be IRIs or blank nodes")
        validate_node(context.identifier)
        if context.identifier != default:
            dataset.graph(cast(URIRef | BNode, own(context.identifier)))
    for count, (subject, predicate, value, graph) in enumerate(source.quads(), 1):
        if count > max_triples:
            raise LimitExceeded("RDF dataset exceeds max_triples")
        name = graph.identifier if isinstance(graph, Graph) else graph
        validate_triple(subject, predicate, value)
        if name is not None:
            validate_node(name)
        if name is None or name == default:
            target = dataset.default_graph
        elif isinstance(name, URIRef | BNode):
            target = dataset.graph(cast(URIRef | BNode, own(name)))
        else:
            raise InputError("RDF graph names must be IRIs or blank nodes")
        target.add((own(subject), own(predicate), own(value)))
    bind_namespaces(dataset, source)
    return dataset


class PreparedSource:
    """An owned RDF document with explicit dataset selection and fidelity status."""

    def __init__(
        self,
        dataset: Dataset,
        *,
        is_dataset: bool,
        document_iri: str | None,
        effective_url: str | None,
        literal_fidelity: str,
        max_triples: int = 1_000_000,
        graph_fidelity: TypingLiteral["complete", "nonempty-only"] = "complete",
        graph_name: str | None = None,
        union: bool = False,
    ) -> None:
        if graph_fidelity not in ("complete", "nonempty-only"):
            raise InputError("Unknown RDF graph-scope fidelity")
        if type(union) is not bool:
            raise InputError("union must be a boolean")
        if graph_name is not None:
            if isinstance(graph_name, BNode):
                raise InputError(
                    "Blank graph names require selection on an owned snapshot"
                )
            if union:
                raise InputError("Choose a graph name or union, not both")
            if graph_name != "@default":
                absolute_iri(graph_name, location="graph_name")
        self.graph_name = graph_name
        self.union = union
        self.graph_fidelity = graph_fidelity
        self._dataset = copy_dataset(dataset, max_triples)
        self._max_triples = max_triples
        self.is_dataset = is_dataset
        self.document_iri = document_iri
        self.effective_url = effective_url
        self.literal_fidelity = literal_fidelity

    def dataset(self) -> Dataset:
        """Export an independent dataset, retaining graph and blank-node relations."""
        return copy_dataset(self._dataset, self._max_triples, standardize_apart=False)

    def graph(
        self, graph_name: GraphSelection | None = None, *, union: bool = False
    ) -> Graph:
        """Export one selected graph; a dataset never silently becomes a union.

        Select ``'@default'`` for the default graph, a named graph IRI/blank node,
        or explicitly request ``union=True``. A graph source needs no selection.
        A declared resource selection is retained and conflicting overrides fail.
        """
        if type(union) is not bool:
            raise InputError("union must be a boolean")
        if self.graph_name is not None or self.union:
            if (graph_name is not None or union) and (
                graph_name != self.graph_name or union != self.union
            ):
                raise InputError(
                    "Graph selection conflicts with the resource declaration"
                )
            graph_name, union = self.graph_name, self.union
        if graph_name is not None and union:
            raise InputError("Choose a graph name or union, not both")
        if self.is_dataset and graph_name is None and not union:
            raise InputError("Dataset requires a graph name or explicit union=True")
        if union:
            graph = Graph(bind_namespaces="none")
            for subject, predicate, value, _ in self._dataset.quads():
                graph.add((subject, predicate, value))
            bind_namespaces(graph, self._dataset)
            return copy_graph(graph, max_triples=self._max_triples)
        if graph_name is None or graph_name == "@default":
            graph = self._dataset.default_graph
        else:
            name = (
                graph_name
                if isinstance(graph_name, BNode)
                else URIRef(absolute_iri(str(graph_name)))
            )
            if name not in {item.identifier for item in self._dataset.graphs()}:
                raise InputError("Selected graph is absent from the supplied dataset")
            graph = self._dataset.graph(name)
        exported = copy_graph(
            graph, max_triples=self._max_triples, standardize_apart=False
        )
        bind_namespaces(exported, self._dataset)
        return exported
