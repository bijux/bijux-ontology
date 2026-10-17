"""Private bounded JSON protocol for an isolated optional SHACL backend."""

import json
import sys
import warnings
from dataclasses import replace
from typing import Any

from rdflib import Graph

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.loading import prepare_rdf


def execute(request: dict[str, Any]) -> dict[str, Any]:
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
    dataset = prepare_rdf(
        request["graphs"],
        format="trig",
        limits=replace(limits, max_triples=limits.max_triples * 2),
    ).dataset()
    data = dataset.graph("urn:bijux:shacl:data")
    shapes = dataset.graph("urn:bijux:shacl:shapes")
    authored = dataset.graph("urn:bijux:shacl:authored-shapes")
    ontology = (
        dataset.graph("urn:bijux:shacl:ontology") if request["has_ontology"] else None
    )
    backend = f"pyshacl {pyshacl.__version__}"
    try:
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
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
                use_shapes=request["use_shapes"] or None,
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
        return {
            "status": "result",
            "backend": backend,
            "conforms": bool(conforms),
            "shapes_count": shapes_count,
            "report": report.serialize(format="nt"),
            "warnings": [
                str(item.message)
                for item in observed
                if isinstance(item.message, (ValidationWarning, ConstraintLoadWarning))
            ],
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
