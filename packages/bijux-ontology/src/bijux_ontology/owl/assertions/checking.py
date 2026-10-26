"""Explicit selected-source orchestration of finite assertion witness rules."""

from __future__ import annotations

import threading
from typing import Literal as Choice

from rdflib import BNode, Graph, Literal
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .context import AssertionContext
from .propagation import close_identities
from .types import AssertionWitnesses
from .witnesses import differences, disjoint, edge_witnesses, negatives, type_witnesses


def check_assertion_witnesses(
    source: Graph | PreparedSource,
    *,
    datatype_map: Choice["owl2-datatype-map"] = "owl2-datatype-map",
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> AssertionWitnesses:
    """Find finite contradictions and equality, never general consistency.

    Scope is explicit property assertions/declarations, named actual types,
    same/different individuals, finite named-class keys and negative assertions.
    Only identity consequences iterate; no RDF graph is saturated or rewritten.
    Key witnesses share each property's value independently, with named subjects
    and named object fillers (including proved identity to a name). Missing key
    values are insufficient evidence; keys never make properties functional.

    Literal comparison uses the implemented OWL2 datatype-map regime, with exact
    validated intrinsic string identity. Unsupported values remain unknown.
    Dataset input requires a physical graph or explicit union, preserving pins.
    Blank keys are local to the returned selected-source evidence. A completed
    search reports no_witness_in_selected_scope, never global consistency.
    """
    if type(datatype_map) is not str or datatype_map != "owl2-datatype-map":
        raise InputError("Select the supported owl2-datatype-map regime")
    if type(union) is not bool:
        raise InputError("union must be an explicit boolean")
    operation = Operation(limits, budget, timeout, cancel)
    state = AssertionContext(Graph(), operation, True)
    completed = False
    try:
        if graph_name is not None:
            if not isinstance(graph_name, str) or isinstance(graph_name, Literal):
                raise InputError("Graph selection requires a graph name or @default")
            bound_text(graph_name, operation)
        prepared = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, budget=operation)
        )
        chosen = prepared.graph_name if graph_name is None else graph_name
        if isinstance(chosen, BNode) and not any(
            actual is chosen for actual in prepared._graph_names(budget=operation)
        ):
            raise InputError("Blank graph selection requires the actual owned term")
        graph = prepared._read(graph_name, union=union, budget=operation)
        state = AssertionContext(
            graph,
            operation,
            prepared.literal_fidelity in ("source_terms", "supplied_terms"),
        )
        for triple in graph:
            operation.consume("work")
            operation.consume("triples")
            for term in triple:
                bound_node(term, operation)
        state.initial()
        functional = state.characteristic(OWL.FunctionalProperty)
        inverse = state.characteristic(OWL.InverseFunctionalProperty)
        irreflexive = state.characteristic(OWL.IrreflexiveProperty)
        asymmetric = state.characteristic(OWL.AsymmetricProperty)
        for predicate in state.characteristic(OWL.DatatypeProperty):
            state.rows(predicate)
        keys = state.patterns.keys()
        close_identities(state, functional, inverse, keys)
        edge_witnesses(state, irreflexive, asymmetric)
        disjoint(state)
        negatives(state)
        differences(state)
        type_witnesses(state)
        completed = True
    except LimitExceeded as error:
        state.issues.exhausted(str(error))
    return AssertionWitnesses(
        tuple(state.witnesses),
        tuple(state.identity.consequences),
        Report(
            "owl.assertion_witnesses",
            int(completed),
            tuple(state.issues),
            requested=1,
            allow_empty=True,
            children=tuple(state.children),
            regime=datatype_map,
            families=("finite-assertion-witnesses",),
        ),
    )
