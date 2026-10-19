"""Owned primary and merged assertion views without cross-filling field contracts."""

from dataclasses import dataclass

from rdflib import BNode, Dataset, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import PartitionPlan, capture, graph_name


class PartitionUnavailable(InputError):
    """Required record or partition scope cannot be established from supplied data."""


@dataclass(frozen=True)
class PartitionInputs:
    """New owned effective containers sharing only exact immutable source terms."""

    primary: PreparedSource
    merged: PreparedSource
    focus: tuple[URIRef | BNode, ...]


def prepare_inputs(
    source: PreparedSource, plan: PartitionPlan, budget: Budget
) -> PartitionInputs:
    """Read only authorized physical partitions and retain intentional blank sharing."""
    if not isinstance(source, PreparedSource) or not isinstance(plan, PartitionPlan):
        raise InputError(
            "Partition validation requires a prepared source and typed plan"
        )
    if source.union or source.literal_fidelity not in (
        "source_terms",
        "supplied_terms",
    ):
        raise PartitionUnavailable(
            "Partitions require exact physical source assertions"
        )
    primary_name = graph_name(plan.primary, budget)
    supports = tuple(
        graph_name(value, budget) for value in capture(plan.support, budget)
    )
    if not source.is_dataset and (primary_name is not None or supports):
        raise InputError("A plain graph has only its primary physical scope")
    if source.is_dataset and primary_name is None:
        raise InputError("A dataset requires an explicit primary graph")
    selected = (primary_name, *supports)
    owned_graphs: set[int] = set()
    if any(isinstance(name, BNode) for name in selected):
        for owned_name in source._graph_names(budget=budget):
            budget.consume("states")
            owned_graphs.add(id(owned_name))
    seen = set()
    for name in selected:
        budget.consume("work")
        if isinstance(name, BNode) and id(name) not in owned_graphs:
            raise InputError("Blank graph selectors require exact snapshot ownership")
        graph_key = ("blank", id(name)) if isinstance(name, BNode) else ("name", name)
        if graph_key in seen:
            raise InputError("Primary and support graph selections must be distinct")
        budget.consume("states")
        seen.add(graph_key)
    primary, merged = Dataset(), Dataset()
    retained: tuple[set[object], set[object]] = (set(), set())
    subjects: set[object] = set()
    blank_subjects: set[int] = set()
    for index, name in enumerate(selected):
        budget.consume("resources")
        graph = source._read(name, budget=budget)
        for prefix, namespace in graph.namespaces():
            budget.consume("states")
            bound_text(prefix, budget)
            bound_node(namespace, budget)
            merged.bind(prefix, namespace)
            if index == 0:
                primary.bind(prefix, namespace)
        for triple in graph:
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
            identity = tuple(term_key(node) for node in triple)
            if index == 0:
                budget.consume("states")
                subjects.add(term_key(triple[0]))
                if isinstance(triple[0], BNode):
                    blank_subjects.add(id(triple[0]))
            for destination, keys in (
                ((primary, retained[0]), (merged, retained[1]))
                if index == 0
                else ((merged, retained[1]),)
            ):
                budget.consume("work")
                if identity not in keys:
                    budget.consume("states")
                    budget.consume("triples")
                    budget.consume("bytes", 96)
                    keys.add(identity)
                    destination.default_graph.add(triple)
    focus = []
    selected_focus = set()
    for focus_node in capture(plan.focus_nodes, budget):
        if not isinstance(focus_node, URIRef | BNode):
            raise InputError("Primary focus inventory requires resource subjects")
        bound_node(focus_node, budget)
        if isinstance(focus_node, BNode) and id(focus_node) not in blank_subjects:
            raise InputError("Blank primary records require exact subject ownership")
        focus_key = term_key(focus_node)
        if focus_key not in subjects:
            raise PartitionUnavailable("A required record is absent from primary data")
        if focus_key in selected_focus:
            raise InputError("Primary record inventory must be distinct")
        budget.consume("states")
        focus.append(focus_node)
        selected_focus.add(focus_key)
    if not focus:
        raise PartitionUnavailable("Partition validation requires primary records")
    for metadata in (source.document_iri, source.effective_url):
        if metadata is not None:
            bound_text(metadata, budget)

    def owned(dataset: Dataset) -> PreparedSource:
        """Adopt new storage while retaining exact original focus term objects."""
        return PreparedSource._from_owned(
            dataset,
            is_dataset=False,
            document_iri=source.document_iri,
            effective_url=source.effective_url,
            literal_fidelity=source.literal_fidelity,
            max_triples=budget.limits.max_triples,
        )

    return PartitionInputs(owned(primary), owned(merged), tuple(focus))
