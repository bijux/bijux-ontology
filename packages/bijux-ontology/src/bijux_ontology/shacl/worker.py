"""Private bounded JSON protocol for an isolated optional SHACL backend."""

import json
import sys
import warnings
from dataclasses import replace
from typing import Any

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF, SH

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.shacl.bindings import read_bindings
from bijux_ontology.shacl.coverage import TARGET
from bijux_ontology.shacl.functions import register_functions


def target_coverage(
    data: Graph,
    shapes: Graph,
    ontology: Graph | None,
    focus_nodes: list[str],
    limits: Limits,
    *,
    validation_complete: bool,
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
    budget = Budget(limits)
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


def execute(request: dict[str, Any]) -> dict[str, Any]:
    """Execute only supplied graph objects, never URLs or backend file loaders."""
    try:
        import pyshacl
        from pyshacl.constraints import ALL_CONSTRAINT_PARAMETERS
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
    dataset = prepare_rdf(
        request["graphs"],
        format="trig",
        limits=replace(
            limits,
            max_triples=limits.max_triples * 2 + request.get("binding_count", 0),
        ),
    ).dataset()
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
            if request["selected_shapes"]:
                # Check authored identity before rewritten targets can promote
                # an arbitrary metadata resource to a vacuously valid shape.
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
                                "Selected resource is not an authored SHACL shape: "
                                f"{root}"
                            ),
                            "backend": backend,
                        }
                # Untyped roots must survive removal of their original targets,
                # including targetless shapes declared only through parameters.
                for root in roots:
                    kind = (
                        SH.PropertyShape
                        if (root, SH.path, None) in authored
                        else SH.NodeShape
                    )
                    shapes.add((root, RDF.type, kind))
                    if request.get("pair_mode"):
                        for node in focus_terms:
                            shapes.add((root, SH.targetNode, node))
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
            with register_functions(
                dataset.graph("urn:bijux:shacl:functions"),
                request.get("functions", ()),
                data,
                limits,
            ) as function_calls:
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


def main() -> int:
    """Read one request and emit one size-bounded response without progress text."""
    try:
        # Parent and child independently enforce the private transport bound.
        cap = int(sys.argv[1])
        if cap <= 0:
            raise InputError("Invalid protocol byte bound")
        payload = sys.stdin.buffer.read(cap + 1)
        if len(payload) > cap:
            raise InputError("SHACL request exceeds protocol byte bound")
        request = decode_json(payload, max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Expected SHACL request object")
        response = execute(request)
        encoded = json.dumps(response, ensure_ascii=True).encode()
        if len(encoded) > cap:
            encoded = json.dumps(
                {
                    "status": "exhausted",
                    "message": "SHACL response exceeds protocol byte bound",
                }
            ).encode()
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except LimitExceeded as error:
        sys.stdout.write(
            json.dumps({"status": "exhausted", "message": str(error)[:2000]})
        )
        return 0
    except InputError as error:
        sys.stdout.write(
            json.dumps({"status": "input_error", "message": str(error)[:2000]})
        )
        return 0
    except Exception as error:
        # Unexpected backend errors cross the process boundary as exceptions,
        # rather than being reclassified as ordinary data nonconformance.
        response = {
            "status": "exception",
            "type": type(error).__name__,
            "message": str(error)[:2000],
        }
        sys.stdout.write(json.dumps(response))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
