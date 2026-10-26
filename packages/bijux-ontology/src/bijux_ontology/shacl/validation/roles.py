"""Exact physical SHACL graph roles and independent per-request focus selection."""

from collections.abc import Mapping, Sequence

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import _charge_graph
from bijux_ontology.rdf.sources.snapshots import (
    GraphSelection,
    PreparedSource,
    copy_graph,
)
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.validation.bindings import TermSelection, resolve_terms
from bijux_ontology.shacl.validation.contracts import ShaclPlan
from bijux_ontology.vocabulary.sources.loading import load_vocabulary


def prepare_role(
    source: Graph | PreparedSource,
    *,
    graph_name: GraphSelection | None,
    union: bool,
    limits: Limits,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    resolve_imports: bool = False,
    budget: Budget | None = None,
) -> Graph:
    """Capture the selected physical graph and explicitly requested import closure."""
    if isinstance(source, PreparedSource):
        graph = source._read(graph_name, union=union, budget=budget)
        _charge_graph(
            graph, budget if budget is not None else Budget(limits), limits.max_bytes
        )
        if resolve_imports:
            graph = copy_graph(
                graph, max_triples=limits.max_triples, standardize_apart=False
            )
    elif graph_name is None and not union and not isinstance(source, Dataset):
        _charge_graph(
            source, budget if budget is not None else Budget(limits), limits.max_bytes
        )
        graph = copy_graph(
            source, max_triples=limits.max_triples, standardize_apart=False
        )
    else:
        graph = prepare_source(source, limits=limits, budget=budget)._read(
            graph_name, union=union, budget=budget
        )
    if resolve_imports and next(graph.objects(None, OWL.imports), None) is not None:
        closure = load_vocabulary(graph, imports=imports, limits=limits, budget=budget)
        for imported in closure.imports:
            graph += imported
    return graph


def select_roots(
    shapes: Graph, selected: tuple[URIRef | BNode | Literal, ...], mode: str
) -> Graph:
    """Select supported Core targets without disabling referenced nested shapes.

    The backend's use_shapes filter omits nested PropertyShapes when only their
    parent is selected. Altering only owned Core target declarations preserves
    those constraints. Pair mode deliberately replaces root targeting.
    """
    if not selected:
        return shapes
    graph = copy_graph(shapes, standardize_apart=False)
    roots = set(selected)
    # Match the maintained backend's finite class-target discovery, including
    # its built-in OWL class declaration, before narrowing root obligations.
    class_types = {RDFS.Class, OWL.Class} | set(
        shapes.subjects(RDFS.subClassOf, RDFS.Class)
    )
    for kind in class_types:
        for shape in tuple(graph.subjects(RDF.type, kind)):
            graph.remove((shape, RDF.type, kind))
            graph.add((shape, SH.targetClass, shape))
    for predicate in (
        SH.targetClass,
        SH.targetNode,
        SH.targetSubjectsOf,
        SH.targetObjectsOf,
        SH.target,
    ):
        for shape in tuple(graph.subjects(predicate)):
            if mode == "pairs" or shape not in roots:
                graph.remove((shape, predicate, None))
    return graph


def prepare_data(
    data: Graph | PreparedSource,
    plan: ShaclPlan,
    focus_nodes: Sequence[TermSelection],
    *,
    graph_name: GraphSelection | None,
    union: bool,
) -> tuple[Graph, tuple[URIRef | BNode | Literal, ...]]:
    """Resolve one independent dataset and its exact owned focus terms."""
    if plan.mode == "pairs" and not focus_nodes:
        raise InputError("Pairs mode requires focus_nodes and use_shapes")
    if plan.mode == "targets" and focus_nodes and plan.roots:
        raise InputError(
            "Combined node/shape selection bypasses targets; choose mode='pairs'"
        )
    if not isinstance(data, (Graph, PreparedSource)):
        raise InputError("SHACL data requires a graph or prepared source")
    dg = prepare_role(
        data, graph_name=graph_name, union=union, limits=plan.limits, budget=plan.budget
    )
    focus = resolve_terms(focus_nodes, data, dg, limits=plan.limits, budget=plan.budget)
    if plan.mode == "targets" and any(not isinstance(node, URIRef) for node in focus):
        raise InputError("Literal and blank focus terms require explicit pairs mode")
    return dg, focus


def role_graphs(plan: ShaclPlan) -> dict[str, Graph]:
    """Keep authored shape identity separate from executable root targets."""
    graphs = {
        "shapes": select_roots(plan.shapes, plan.roots, plan.mode),
        "authored-shapes": plan.shapes,
    }
    if plan.ontology is not None:
        graphs["ontology"] = plan.ontology
    if plan.has_functions:
        graphs["functions"] = plan.function_plan.graph
    return graphs


def prepay_graph(graph: Graph, budget: Budget) -> None:
    """Bound term validation and graph-copy workspace before maintained copying."""
    budget.consume("states", len(graph))
    budget.consume("bytes", 256 * len(graph) + 256)
    for triple in graph:
        budget.consume("work")
        for term in triple:
            bound_node(term, budget)
            size = len(str(term))
            if isinstance(term, Literal):
                size += len(term.datatype or "") + len(term.language or "")
            budget.consume("bytes", 16 * size + 64)
            budget.consume("work", size + 1)
