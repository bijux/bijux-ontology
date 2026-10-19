"""Cube normalization in one owned physical graph with snapshot update semantics."""

from collections.abc import Iterable, Iterator
from typing import cast

from rdflib import BNode, Dataset, Graph, Namespace, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, bound_text, validate_triple
from bijux_ontology.reports import Issue

QB = Namespace("http://purl.org/linked-data/cube#")
Triple = tuple[Node, URIRef, Node]


def _add(graph: Graph, triple: Triple, budget: Budget) -> None:
    """Pay retained inferred statements before altering the private check view."""
    budget.consume("work")
    if triple in graph:
        return
    for term in triple:
        bound_node(term, budget)
    validate_triple(*triple)
    budget.consume("triples")
    budget.consume("states")
    budget.consume("bytes", 128)
    graph.add(triple)


def _batch(graph: Graph, triples: Iterable[Triple], budget: Budget) -> None:
    """Each update reads a fixed graph; its own insertions cannot create new joins."""
    pending: list[Triple] = []
    for triple in triples:
        budget.consume("work")
        budget.consume("states")
        budget.consume("bytes", 96)
        pending.append(triple)
    for triple in pending:
        _add(graph, triple, budget)


def _objects(
    graph: Graph, subject: Node, predicate: URIRef, budget: Budget
) -> Iterator[Node]:
    """Charge every join witness, including combinations producing no insertion."""
    for value in graph.objects(subject, predicate):
        budget.consume("work")
        yield value


def _attachments(graph: Graph, level: URIRef, budget: Budget) -> Iterator[Triple]:
    """Generate one exact dataset/slice/dimension pushdown update."""
    for data, _, dsd in graph.triples((None, QB.structure, None)):
        budget.consume("work")
        for spec in _objects(graph, dsd, QB.component, budget):
            for prop in _objects(graph, spec, QB.componentProperty, budget):
                if not isinstance(prop, URIRef):
                    continue
                dimension = (
                    level == QB.DimensionProperty
                    and (prop, RDF.type, QB.DimensionProperty) in graph
                )
                attached = (
                    level in (QB.DataSet, QB.Slice)
                    and (spec, QB.componentAttachment, level) in graph
                )
                if not dimension and not attached:
                    continue
                if level == QB.DataSet:
                    for obs in graph.subjects(QB.dataSet, data):
                        budget.consume("work")
                        for value in _objects(graph, data, prop, budget):
                            yield obs, prop, value
                else:
                    for slice_node in _objects(graph, data, QB.slice, budget):
                        for obs in _objects(graph, slice_node, QB.observation, budget):
                            for value in _objects(graph, slice_node, prop, budget):
                                yield obs, prop, value


def _collection_roots(graph: Graph, budget: Budget) -> Iterator[Node]:
    """Select only collection definitions required by actual dimension values."""
    for obs, _, data in graph.triples((None, QB.dataSet, None)):
        budget.consume("work")
        for dsd in _objects(graph, data, QB.structure, budget):
            for spec in _objects(graph, dsd, QB.component, budget):
                for dim in _objects(graph, spec, QB.componentProperty, budget):
                    if (
                        not isinstance(dim, URIRef)
                        or (dim, RDF.type, QB.DimensionProperty) not in graph
                    ):
                        continue
                    if next(_objects(graph, obs, dim, budget), None) is None:
                        continue
                    for code_list in _objects(graph, dim, QB.codeList, budget):
                        if any(
                            (code_list, RDF.type, kind) in graph
                            for kind in (SKOS.Collection, SKOS.OrderedCollection)
                        ):
                            yield code_list


def _collections(graph: Graph, budget: Budget, issues: IssueCollector) -> None:
    """Materialize S36 only within required collection membership closure."""
    pending: list[Node] = []
    seen: set[Node] = set()

    def retain(node: Node) -> None:
        """Pay finite closure references before enqueueing a supplied resource."""
        budget.consume("work")
        if node not in seen:
            budget.consume("states", 2)
            budget.consume("bytes", 128)
            seen.add(node)
            pending.append(node)

    for root in _collection_roots(graph, budget):
        retain(root)
    while pending:
        collection = pending.pop()
        for head in _objects(graph, collection, SKOS.memberList, budget):
            try:
                _batch(
                    graph,
                    (
                        (collection, SKOS.member, member)
                        for member in iter_rdf_list(
                            graph, head, max_items=budget.limits.max_work, budget=budget
                        )
                    ),
                    budget,
                )
            except LimitExceeded:
                raise
            except InputError:
                bound_node(collection, budget)
                issues.append(
                    Issue(
                        "cube.constraint_unverified",
                        "Required collection membership list cannot be qualified",
                        str(collection),
                        kind="unverified",
                    )
                )
        for member in _objects(graph, collection, SKOS.member, budget):
            retain(member)


def normalize(
    source: PreparedSource,
    graph_name: GraphSelection | None,
    budget: Budget,
    issues: IssueCollector,
) -> tuple[PreparedSource, GraphSelection | None, SnapshotMetadata]:
    """Apply documented Cube closure and pushdowns without changing asserted input."""
    before = SnapshotMetadata.capture(source, budget)
    if graph_name is not None:
        bound_text(graph_name, budget)
    selected = source.graph_name if source.graph_name is not None else graph_name
    if isinstance(selected, BNode) and not any(
        name is selected for name in source._graph_names(budget=budget)
    ):
        raise InputError("Cube blank graph selection needs exact snapshot ownership")
    original = source._read(graph_name, budget=budget)
    budget.consume("resources")
    budget.consume("states", len(original))
    budget.consume("bytes", 128 * len(original))
    dataset = Dataset()
    target = (
        dataset.default_graph
        if not before.is_dataset or selected == "@default"
        else dataset.graph(
            selected if isinstance(selected, BNode) else URIRef(str(selected))
        )
    )
    for subject, predicate, obj in original:
        _add(target, (subject, cast(URIRef, predicate), obj), budget)
    _batch(
        target,
        (
            (obs, RDF.type, QB.Observation)
            for _, _, obs in target.triples((None, QB.observation, None))
        ),
        budget,
    )
    _batch(
        target,
        (
            triple
            for obs, _, data in target.triples((None, QB.dataSet, None))
            for triple in (
                (obs, RDF.type, QB.Observation),
                (data, RDF.type, QB.DataSet),
            )
        ),
        budget,
    )
    _batch(
        target,
        (
            (slice_node, RDF.type, QB.Slice)
            for _, _, slice_node in target.triples((None, QB.slice, None))
        ),
        budget,
    )
    for alias, kind in (
        (QB.dimension, QB.DimensionProperty),
        (QB.measure, QB.MeasureProperty),
        (QB.attribute, QB.AttributeProperty),
    ):
        _batch(
            target,
            (
                triple
                for spec, _, prop in target.triples((None, alias, None))
                for triple in (
                    (spec, QB.componentProperty, prop),
                    (prop, RDF.type, kind),
                )
            ),
            budget,
        )
    for level in (QB.DataSet, QB.Slice, QB.DimensionProperty):
        _batch(target, _attachments(target, level, budget), budget)
    _collections(target, budget, issues)
    if not before.same_scope(SnapshotMetadata.capture(source, budget)):
        raise InputError("Cube source metadata changed during normalization")
    normalized = PreparedSource._from_owned(
        dataset,
        is_dataset=before.is_dataset,
        document_iri=before.document_iri,
        effective_url=before.effective_url,
        literal_fidelity=before.literal_fidelity,
        max_triples=budget.limits.max_triples,
        graph_fidelity="complete",
        graph_name=None if isinstance(selected, BNode) else selected,
    )
    return normalized, selected, before
