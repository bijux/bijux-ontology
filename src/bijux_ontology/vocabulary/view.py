"""Owned local, required-import and lookup vocabulary snapshots."""

from collections.abc import Sequence

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import bind_namespaces, copy_graph


class Vocabulary:
    """Owned local/import/lookup snapshots; public graphs are independent copies.

    Construction always copies supplied graphs and separates independent blank
    nodes. Internal indexes are never exposed as mutable graph handles.
    """

    def __init__(
        self,
        local: Graph,
        imports: Sequence[Graph] = (),
        lookup: Sequence[Graph] = (),
        *,
        limits: Limits | None = None,
    ) -> None:
        selected = limits if limits is not None else Limits()
        budget = Budget(selected)
        self._limits = selected

        def own(graph: Graph) -> Graph:
            return prepare_rdf(graph, limits=selected, budget=budget).graph()

        self._local = own(local)
        self._imports = tuple(own(item) for item in imports)
        self._lookup = tuple(own(item) for item in lookup)
        self._combined = Graph(bind_namespaces="none")
        for graph in (self._local, *self._imports, *self._lookup):
            self._combined += graph
        bind_namespaces(self._combined, self._local, *self._imports, *self._lookup)
        self._kind_index = _index_kinds(self._combined)
        self._local_kind_index = _index_kinds(self._local)
        closure = Graph(bind_namespaces="none")
        for graph in (self._local, *self._imports):
            closure += graph
        self._closure_kind_index = _index_kinds(closure)

    @property
    def limits(self) -> Limits:
        """Return the immutable bounds used to prepare this vocabulary."""
        return self._limits

    @property
    def local(self) -> Graph:
        """Export local declarations without permitting mutation of this view."""
        return copy_graph(self._local, standardize_apart=False)

    @property
    def imports(self) -> tuple[Graph, ...]:
        """Export owned required-import graphs, separate from local declarations."""
        return tuple(
            copy_graph(item, standardize_apart=False) for item in self._imports
        )

    @property
    def lookup(self) -> tuple[Graph, ...]:
        """Export descriptive lookup sources without changing import ownership."""
        return tuple(copy_graph(item, standardize_apart=False) for item in self._lookup)

    def graph(self) -> Graph:
        """Export an independent union of explicitly selected source scopes."""
        return copy_graph(self._combined, standardize_apart=False)


def _index_kinds(graph: Graph) -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for subject, kind in graph.subject_objects(RDF.type):
        if isinstance(subject, URIRef) and isinstance(kind, URIRef):
            index.setdefault(str(subject), set()).add(str(kind))
    return {iri: frozenset(kinds) for iri, kinds in index.items()}
