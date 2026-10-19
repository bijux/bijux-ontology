"""Finite data candidates checked only by the maintained SHACL backend."""

import time

from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.execution.session import ShaclSession
from bijux_ontology.shacl.refinement.normalization import (
    Branch,
    Obligation,
    Unsupported,
)
from bijux_ontology.shacl.refinement.types import RefinementContract

_BASE = "urn:bijux:ontology:refinement:"


def _values(item: Obligation, alternative: int, budget: Operation) -> tuple[Node, ...]:
    """Choose original terms without implementing data validation."""
    budget.consume("states", len(item.fixed))
    values = {term_key(value): value for value in item.fixed}
    needed = max(item.minimum, len(values))
    if item.maximum is not None and needed > item.maximum:
        return ()
    if item.allowed is not None:
        candidates = item.allowed
        if candidates:
            shift = alternative % len(candidates)
            budget.consume("states", len(candidates))
            candidates = candidates[shift:] + candidates[:shift]
        for value in candidates:
            budget.consume("work")
            if len(values) >= needed:
                break
            budget.consume("states")
            values[term_key(value)] = value
    else:
        for index in range(needed):
            budget.consume("work")
            budget.consume("states")
            if len(values) >= needed:
                break
            if item.datatypes:
                datatype = URIRef(item.datatypes[0])
                text = (
                    str(index + alternative)
                    if datatype != XSD.boolean
                    else ("true" if index % 2 else "false")
                )
                literal = Literal(text, datatype=datatype, normalize=False)
                bound_node(literal, budget)
                if not check_literal(literal, budget=budget).verified:
                    raise Unsupported(
                        "A datatype has no qualified finite witness representative"
                    )
                value = literal
            elif "iri" in item.kinds:
                value = URIRef(f"{_BASE}value:{alternative}:{index}")
            elif "literal" in item.kinds:
                value = Literal(f"value:{alternative}:{index}")
            else:
                value = BNode()
            values[term_key(value)] = value
    for value in values.values():
        budget.consume("work")
        bound_node(value, budget)
        if (
            isinstance(value, Literal)
            and not check_literal(value, budget=budget).verified
        ):
            raise Unsupported("A witness literal lacks qualified lexical semantics")
    return tuple(values.values())


def candidate(
    branch: Branch,
    contract: RefinementContract,
    focus: URIRef,
    classes: tuple[str, ...],
    alternative: int,
    budget: Operation,
) -> PreparedSource:
    """Materialize direct/inverse path candidates without a competing path engine."""
    budget.consume("resources")
    dataset = Dataset()
    graph = (
        dataset.default_graph
        if contract.data_graph is None
        else dataset.graph(contract.data_graph)
    )
    triples: list[tuple[Node, Node, Node]] = [
        (focus, URIRef(_BASE + "present"), Literal(True))
    ]
    budget.consume("states", len(classes) + 1)
    triples.extend((focus, RDF.type, URIRef(iri)) for iri in classes)
    for item in branch:
        budget.consume("work")
        path = item.path
        inverse = (
            path.kind == "inverse"
            and len(path.children) == 1
            and path.children[0].kind == "predicate"
        )
        if path.kind != "predicate" and not inverse:
            raise Unsupported(
                "Witness construction requires predicate or inverse-predicate paths"
            )
        predicate = path.children[0].node if inverse else path.node
        for value in _values(item, alternative, budget):
            budget.consume("states")
            triple: tuple[Node, Node, Node] = (
                (value, predicate, focus) if inverse else (focus, predicate, value)
            )
            if inverse and isinstance(value, Literal):
                raise Unsupported("An inverse witness cannot use a literal subject")
            triples.append(triple)
    for triple in triples:
        budget.consume("triples")
        for node in triple:
            bound_node(node, budget)
        graph.add(triple)
    if contract.data_graph is None:
        return prepare_rdf(graph, budget=budget)
    return prepare_rdf(
        RdfSource(dataset, graph_name=str(contract.data_graph)), budget=budget
    )


def run(
    contract: RefinementContract,
    data: PreparedSource,
    budget: Operation,
) -> ShaclResult:
    """Check the complete declared target scope under the enclosing deadline."""
    budget.consume("resources")
    remaining = budget.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Profile refinement exceeded its deadline")
    with ShaclSession(
        contract.source,
        use_shapes=(contract.root,),
        mode="targets",
        preflight=True,
        timeout=remaining,
        limits=budget.limits,
    ) as session:
        return session._validate(
            data,
            focus_nodes=(),
            graph_name=None,
            union=False,
            source_usages=(),
            scope="dataset",
            cancel=budget.cancel,
            budget=budget,
        )
