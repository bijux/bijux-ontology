"""One isolated maintained rule program with same-scope convergence accounting."""

import sys
import warnings
from contextlib import redirect_stdout
from dataclasses import replace

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SH

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.patches.codecs import read_witness
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import SnapshotWitness
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.sparql.functions import register_functions

from .preparation import (
    RuleUnavailable,
    bind_namespaces,
    function_plan,
    graph_from_witness,
    qualify_program,
    selected_names,
)
from .protocol import (
    RuleRequest,
    completed_execution,
    failed_execution,
    read_request,
    response_bytes,
)


def _identity(graph: Graph, budget: Budget) -> set[tuple[tuple[str | None, ...], ...]]:
    """Bound actual retained RDF and compare intrinsic terms in this worker scope."""
    if len(graph) > budget.limits.max_triples:
        raise LimitExceeded("Rule materialization exceeds max_triples")
    result = set()
    for triple in graph:
        budget.consume("work")
        budget.consume("states")
        for node in triple:
            bound_node(node, budget)
        result.add(tuple(term_key(node) for node in triple))
    return result


def _result(graph: Graph, scope: SnapshotWitness, budget: Budget) -> SnapshotWitness:
    """Capture derived anonymous identity once and restore the original partition."""
    captured = capture_source(
        prepare_rdf(graph, budget=budget), "result:", budget
    ).witness
    budget.consume("work", len(captured.quads))
    budget.consume("states", len(captured.quads))
    return SnapshotWitness(
        scope.kind,
        scope.graph_selection,
        tuple(replace(quad, graph=scope.graph_selection) for quad in captured.quads),
        scope.graphs,
    )


def _execute(request: RuleRequest, budget: Budget) -> tuple[int, SnapshotWitness]:
    """Run full public ordered passes, never the backend's local iteration flag."""
    from pyshacl import shacl_rules
    from pyshacl.graph_abstraction import DataGraph
    from pyshacl.shape import Shape
    from pyshacl.shapes_graph import ShapesGraph

    initial = read_witness(request["data"], budget)
    graph = graph_from_witness(initial, budget)
    shapes = graph_from_witness(read_witness(request["rules"], budget), budget)
    bind_namespaces(shapes, request["namespaces"], budget)
    names = selected_names(request["selected"], budget)
    mode, maximum = request["mode"], request["maximum"]
    if (
        mode not in ("ordered-pass", "global-fixed-point")
        or type(maximum) is not int
        or not 0 < maximum <= budget.limits.max_work
    ):
        raise InputError("Malformed rule execution policy")
    functions = None
    function_names: tuple[str, ...] = ()
    if request["functions"] is not None:
        functions = graph_from_witness(
            read_witness(request["functions"], budget), budget
        )
        bind_namespaces(functions, request["function_namespaces"], budget)
        function_names = selected_names(request["function_names"], budget)
    plan = function_plan(functions, function_names, budget)
    qualify_program(shapes, names, plan, budget)
    before = _identity(graph, budget)
    passes = 0
    # Only this subprocess registers explicitly selected functions. No catalog is
    # merged into the rules graph where the backend could auto-discover extras.
    with register_functions(
        plan.graph, plan.names, graph, budget.limits, budget=budget
    ):
        while True:
            budget.consume("work")
            with warnings.catch_warnings(), redirect_stdout(sys.stderr):
                warnings.simplefilter("error")
                returned = shacl_rules(
                    graph,
                    shacl_graph=shapes,
                    iterate_rules=False,
                    inference="none",
                    inplace=True,
                    js=False,
                    do_owl_imports=False,
                )
            if not isinstance(returned, Graph) or any(
                returned.triples(
                    (None, RDF.type, URIRef(str(SH) + "ValidationFailure"))
                )
            ):
                raise RuleUnavailable(
                    "The maintained rule executor returned incomplete output"
                )
            after = _identity(graph, budget)
            if not before <= after:
                raise RuleUnavailable(
                    "The rule executor did not preserve prior exact RDF terms"
                )
            budget.consume("triples", len(after) - len(before))
            passes += 1
            if mode == "ordered-pass" or after == before:
                break
            if passes >= maximum:
                raise LimitExceeded("Global rule closure exceeded max_passes")
            before = after
    supplied = ShapesGraph(shapes)
    for name in names:
        budget.consume("work")
        targets = Shape(supplied, URIRef(name)).focus_nodes(  # type: ignore[no-untyped-call]
            DataGraph.from_rdflib(graph)
        )
        budget.consume("states", len(targets))
        if not targets:
            raise RuleUnavailable("A required rule owner has no actual focus nodes")
        for target in targets:
            bound_node(target, budget)
    return passes, _result(graph, initial, budget)


def main() -> int:
    """Read and return bounded typed protocol data; never return a partial closure."""
    try:
        cap = int(sys.argv[1])
        if cap <= 0:
            return 2
        request, budget = read_request(sys.stdin.buffer.read(cap + 1), cap)
        try:
            passes, effective = _execute(request, budget)
            result = completed_execution(passes, effective, budget)
        except Exception as error:
            result = failed_execution(error)
        output = response_bytes(result, budget, cap)
        sys.stdout.buffer.write(output)
        return 0
    except (InputError, ValueError, IndexError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
