"""Qualified reads of owned RDF projections and physical dataset partitions."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal as Kind
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.sources.snapshots import (
    GraphSelection,
    PreparedSource,
)


def _same_name(left: GraphSelection | None, right: GraphSelection | None) -> bool:
    """Blank graph authority belongs to its actual source-owned term."""
    if isinstance(left, Literal) or isinstance(right, Literal):
        return False
    if isinstance(left, BNode) or isinstance(right, BNode):
        return left is right
    if left is None or right is None:
        return left is right
    return str(left) == str(right)


@dataclass(frozen=True)
class SelectedRead:
    """One admitted scope over existing read-only handles, without a second copy."""

    kind: Kind["graph", "default", "named", "dataset", "union"]
    graph_name: URIRef | BNode | None
    _source: PreparedSource
    _reader: Graph | None
    _is_dataset: bool
    _declared_graph: GraphSelection | None
    _declared_union: bool

    def _unchanged(self) -> None:
        source = self._source
        if (
            source.is_dataset is not self._is_dataset
            or source.union is not self._declared_union
            or not _same_name(source.graph_name, self._declared_graph)
        ):
            raise InputError("The owned RDF declaration changed after read selection")

    def read(self) -> Graph:
        """Reuse one already admitted read-only graph, preserving captured scope."""
        self._unchanged()
        if self._reader is None:
            raise InputError("A whole dataset is not one RDF graph")
        return self._reader

    def contexts(self, budget: Budget) -> Iterator[tuple[URIRef | BNode | None, Graph]]:
        """Preserve physical names and empty graphs, or one explicit projection."""
        self._unchanged()
        if self.kind != "dataset":
            yield self.graph_name, self.read()
            self._unchanged()
            return
        default = self._source._read("@default", budget=budget)
        self._unchanged()
        yield None, default
        for name in self._source._graph_names(budget=budget):
            self._unchanged()
            graph = self._source._read(name, budget=budget)
            self._unchanged()
            yield name, graph
        self._unchanged()


def select_read(
    source: PreparedSource,
    budget: Budget,
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    dataset: bool = False,
) -> SelectedRead:
    """Qualify existing source selection, an explicit graph, union or dataset."""
    if not isinstance(source, PreparedSource):
        raise InputError("Owned RDF reads require a PreparedSource")
    if type(union) is not bool or type(dataset) is not bool:
        raise InputError("RDF read selection flags must be booleans")
    budget.consume("states")
    budget.consume("bytes", 112)
    declared_graph, declared_union, is_dataset = (
        source.graph_name,
        source.union,
        source.is_dataset,
    )
    if type(declared_union) is not bool or type(is_dataset) is not bool:
        raise InputError("Owned RDF declaration flags must remain booleans")
    if graph_name is not None:
        if not isinstance(graph_name, str) or isinstance(graph_name, Literal):
            raise InputError(
                "RDF graph selection requires an IRI or an owned blank term"
            )
        bound_text(graph_name, budget)
    if declared_graph is not None:
        if not isinstance(declared_graph, str) or isinstance(declared_graph, Literal):
            raise InputError(
                "Owned RDF graph selection requires an IRI or an owned blank term"
            )
        bound_text(declared_graph, budget)
    kind: Kind["graph", "default", "named", "dataset", "union"]
    if dataset:
        if (
            graph_name is not None
            or union
            or declared_graph is not None
            or declared_union
        ):
            raise InputError("Dataset reading conflicts with the resource selection")
        kind = "dataset" if is_dataset else "graph"
    else:
        effective_graph, effective_union = (
            (declared_graph, declared_union)
            if declared_graph is not None or declared_union
            else (graph_name, union)
        )
        if effective_union:
            kind = "union"
        elif effective_graph is None or effective_graph == "@default":
            kind = "default" if is_dataset else "graph"
        else:
            if not is_dataset:
                raise InputError("A graph source cannot authorize a named graph")
            kind = "named"
    reader = (
        None
        if kind == "dataset"
        else source._read(graph_name, union=union, budget=budget)
    )
    name = (
        cast(URIRef | BNode, reader.identifier)
        if kind == "named" and reader is not None
        else None
    )
    result = SelectedRead(
        kind, name, source, reader, is_dataset, declared_graph, declared_union
    )
    result._unchanged()
    return result


def physical_graph(
    source: PreparedSource, graph: URIRef | BNode | None | Absent, budget: Budget
) -> tuple[URIRef | BNode | None, Graph]:
    """Read one explicit physical graph, inheriting only a declared selection."""
    if not isinstance(source, PreparedSource):
        raise InputError("Physical RDF reads require an owned PreparedSource")
    if source.union:
        raise InputError("Physical RDF reading requires one graph, not a union")
    if graph is ABSENT:
        selected = select_read(source, budget)
    else:
        selected = select_read(
            source, budget, graph_name="@default" if graph is None else graph
        )
    return selected.graph_name, selected.read()


def selected_contexts(
    source: PreparedSource, budget: Budget, *, graph_name: GraphSelection | None = None
) -> Iterator[tuple[URIRef | BNode | None, Graph]]:
    """Enumerate authorized partitions, retaining whole unpinned datasets."""
    selected = select_read(
        source,
        budget,
        graph_name=graph_name,
        dataset=graph_name is None
        and source.is_dataset
        and source.graph_name is None
        and not source.union,
    )
    yield from selected.contexts(budget)
