"""Native SHACL semantics over supplied role graphs and fixed shape contracts."""

import warnings
from dataclasses import replace
from typing import Any

from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.namespace import RDF, SH

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.shacl.bindings import read_bindings
from bijux_ontology.shacl.coverage import TARGET
from bijux_ontology.shacl.functions import register_functions
from bijux_ontology.shacl.targets.execution import materialize_targets


def target_coverage(
    data: Graph,
    shapes: Graph,
    ontology: Graph | None,
    focus_nodes: list[str],
    limits: Limits,
    *,
    validation_complete: bool,
    budget: Budget | None = None,
) -> dict[str, object]:
    """Enumerate the completed backend run's root targets on its effective graph."""
    from pyshacl import Validator
    from pyshacl.graph_abstraction import DataGraph

    validator = Validator(
        DataGraph.from_rdflib(data),
        shacl_graph=shapes,
        ont_graph=ontology,
        options={"inference": "none", "advanced": False, "use_js": False},
    )
    effective = validator.data_graph
    if ontology is not None:
        effective = validator.mix_in_ontology()  # type: ignore[no-untyped-call]
    focus = {URIRef(iri) for iri in focus_nodes}
    graph = Graph()
    budget = budget if budget is not None else Budget(limits)
    complete = validation_complete
    exhausted = False
    message = "" if complete else "Backend warnings prevent completed target coverage"
    try:
        for shape in sorted(
            validator.shacl_graph.shapes, key=lambda item: item.node.n3()
        ):
            budget.consume("work")
            if shape.deactivated:
                budget.consume("triples")
                graph.add((TARGET.scope, TARGET.deactivated, shape.node))
                continue
            nodes = shape.focus_nodes(effective)
            for node in sorted(nodes, key=lambda item: item.n3()):
                budget.consume("work")
                if focus and node not in focus:
                    continue
                budget.consume("states")
                budget.consume("triples", 2)
                record = BNode()
                graph.add((record, TARGET.shape, shape.node))
                graph.add((record, TARGET.focus, node))
    except LimitExceeded as error:
        complete = False
        exhausted = True
        message = str(error)
    return {
        "graph": graph.serialize(format="nt"),
        "complete": complete,
        "exhausted": exhausted,
        "message": message,
    }


def read_roles(request: dict[str, Any], budget: Budget | None = None) -> Dataset:
    """Parse bounded role quads; fixed protocol roles supply empty graph identity.

    No source graph-declaration inventory is needed for these private datasets.
    Standalone parsing retains its transport ceiling; an owning shared operation
    also charges this preparation to its original cumulative allowance.
    """
    limits = Limits(**request["limits"])
    transport = replace(
        limits,
        max_triples=limits.max_triples * 5
        + request.get("binding_count", 0)
        + request.get("anchor_count", 0),
        max_bytes=limits.max_total_bytes,
    )
    return prepare_rdf(
        request["graphs"],
        format="nquads",
        limits=limits if budget is not None else transport,
        budget=budget,
        max_triples=transport.max_triples,
        max_bytes=transport.max_bytes,
    ).dataset()


def prepare_shapes(
    request: dict[str, Any],
    authored: Graph,
    shapes: Graph,
    roots: tuple[Any, ...],
    backend: str,
    budget: Budget | None = None,
) -> dict[str, Any] | None:
    """Check fixed authored identities and meta-shapes before job targeting."""
    import pyshacl
    from pyshacl.constraints import ALL_CONSTRAINT_PARAMETERS
    from pyshacl.shapes_graph import ShapesGraph

    if request["selected_shapes"]:
        if budget is not None:
            budget.consume("work", len(authored) + len(roots))
            budget.consume("states", len(authored) + len(roots))
        discovered = {shape.node for shape in ShapesGraph(authored).shapes}
        parameters = set(ALL_CONSTRAINT_PARAMETERS)
        discovered.update(
            subject
            for subject, predicate in authored.subject_predicates()
            if predicate in parameters
        )
        for root in roots:
            if root not in discovered:
                return {
                    "status": "input_error",
                    "message": (
                        f"Selected resource is not an authored SHACL shape: {root}"
                    ),
                    "backend": backend,
                }
            kind = (
                SH.PropertyShape if (root, SH.path, None) in authored else SH.NodeShape
            )
            shapes.add((root, RDF.type, kind))
    if request["preflight"]:
        pyshacl.validate(
            Graph(),
            shacl_graph=authored,
            inference="none",
            advanced=False,
            js=False,
            do_owl_imports=False,
            meta_shacl=True,
        )
    return None


def execute(
    request: dict[str, Any],
    *,
    supplied: Dataset | None = None,
    prepared: bool = False,
    budget: Budget | None = None,
) -> dict[str, Any]:
    """Execute only supplied graph objects, never URLs or backend file loaders."""
    try:
        import pyshacl
        from pyshacl.errors import (
            ConstraintLoadError,
            ConstraintLoadWarning,
            ReportableRuntimeError,
            ShapeLoadError,
            ValidationFailure,
            ValidationWarning,
        )
        from pyshacl.shapes_graph import ShapesGraph
    except ImportError:
        return {"status": "unavailable", "message": "Install bijux-ontology[shacl]"}
    limits = Limits(**request["limits"])
    dataset = supplied if supplied is not None else read_roles(request, budget)
    if budget is not None:
        bindings = dataset.graph("urn:bijux:shacl:bindings")
        budget.consume("work", len(bindings))
        budget.consume("states", len(bindings))
    focus_terms, roots = read_bindings(dataset)
    data = dataset.graph("urn:bijux:shacl:data")
    shapes = dataset.graph("urn:bijux:shacl:shapes")
    authored = dataset.graph("urn:bijux:shacl:authored-shapes")
    ontology = (
        dataset.graph("urn:bijux:shacl:ontology") if request["has_ontology"] else None
    )
    backend = f"pyshacl {pyshacl.__version__}"
    function_calls: dict[str, int] = {}
    try:
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            if not prepared:
                problem = prepare_shapes(
                    request, authored, shapes, roots, backend, budget
                )
                if problem is not None:
                    return problem
            if request.get("pair_mode"):
                if budget is not None:
                    count = len(roots) * len(focus_terms)
                    budget.consume("work", count)
                    budget.consume("triples", count)
                    budget.consume("states", count)
                for root in roots:
                    for node in focus_terms:
                        shapes.add((root, SH.targetNode, node))
            with register_functions(
                dataset.graph("urn:bijux:shacl:functions"),
                request.get("functions", ()),
                data,
                limits,
                budget=budget,
            ) as function_calls:
                if request.get("trusted_targets"):
                    materialize_targets(data, shapes, ontology, limits, budget=budget)
                conforms, report, _ = pyshacl.validate(
                    data,
                    shacl_graph=shapes,
                    ont_graph=ontology,
                    inference="none",
                    advanced=False,
                    js=False,
                    iterate_rules=False,
                    inplace=False,
                    abort_on_first=False,
                    allow_infos=False,
                    allow_warnings=False,
                    do_owl_imports=False,
                    sparql_mode=False,
                    max_validation_depth=request["max_depth"],
                    focus_nodes=request["focus_nodes"] or None,
                    use_shapes=None,
                    meta_shacl=False,
                )
        # Use the maintained backend discovery, including implicit property shapes.
        shapes_count = len(ShapesGraph(shapes).shapes)
        if isinstance(report, ValidationFailure):
            return {"status": "failure", "message": str(report), "backend": backend}
        if not isinstance(report, Graph):
            raise RuntimeError("SHACL backend returned a non-RDF report")
        if len(report) > limits.max_triples:
            return {
                "status": "exhausted",
                "message": "SHACL report exceeds max_triples",
                "backend": backend,
                "conforms": bool(conforms),
            }
        result = {
            "status": "result",
            "backend": backend,
            "conforms": bool(conforms),
            "shapes_count": shapes_count,
            "report": report.serialize(format="nt"),
            "functions": {"registered": list(function_calls), "calls": function_calls},
            "warnings": [
                str(item.message)
                for item in observed
                if isinstance(item.message, (ValidationWarning, ConstraintLoadWarning))
            ],
        }
        if request.get("coverage"):
            result["coverage"] = target_coverage(
                data,
                shapes,
                ontology,
                request["focus_nodes"],
                limits,
                validation_complete=not result["warnings"],
                budget=budget,
            )
        return result
    except LimitExceeded as error:
        return {
            "status": "exhausted",
            "message": str(error),
            "backend": backend,
            "functions": {"registered": list(function_calls), "calls": function_calls},
        }
    except (ShapeLoadError, ConstraintLoadError) as error:
        return {"status": "shapes_invalid", "message": str(error), "backend": backend}
    except ReportableRuntimeError as error:
        status = (
            "exhausted"
            if str(error).startswith("Validation path too deep!\n")
            else "shapes_invalid"
            if request["preflight"]
            and str(error).startswith("SHACL File does not validate")
            else "failure"
        )
        return {"status": status, "message": str(error), "backend": backend}
