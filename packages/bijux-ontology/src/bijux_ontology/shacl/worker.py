"""Private bounded JSON protocol for an isolated optional SHACL backend."""

import json
import sys
import warnings
from dataclasses import replace
from typing import Any

from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.shacl.bindings import ANCHOR, SELECTION, read_bindings
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


def _roles(request: dict[str, Any]) -> Dataset:
    """Parse bounded role quads; fixed protocol roles supply empty graph identity.

    No source graph-declaration inventory is needed for these private datasets.
    Its separate parser work must not consume constraint or target allowances.
    """
    limits = Limits(**request["limits"])
    return prepare_rdf(
        request["graphs"],
        format="nquads",
        limits=replace(
            limits,
            max_triples=limits.max_triples * 5
            + request.get("binding_count", 0)
            + request.get("anchor_count", 0),
            max_bytes=limits.max_total_bytes,
        ),
    ).dataset()


def _prepare_shapes(
    request: dict[str, Any],
    authored: Graph,
    shapes: Graph,
    roots: tuple[Any, ...],
    backend: str,
) -> dict[str, Any] | None:
    """Check fixed authored identities and meta-shapes before job targeting."""
    import pyshacl
    from pyshacl.constraints import ALL_CONSTRAINT_PARAMETERS
    from pyshacl.shapes_graph import ShapesGraph

    if request["selected_shapes"]:
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
    request: dict[str, Any], *, supplied: Dataset | None = None, prepared: bool = False
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
    dataset = supplied if supplied is not None else _roles(request)
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
                problem = _prepare_shapes(request, authored, shapes, roots, backend)
                if problem is not None:
                    return problem
            if request.get("pair_mode"):
                for root in roots:
                    for node in focus_terms:
                        shapes.add((root, SH.targetNode, node))
            with register_functions(
                dataset.graph("urn:bijux:shacl:functions"),
                request.get("functions", ()),
                data,
                limits,
            ) as function_calls:
                if request.get("trusted_targets"):
                    materialize_targets(data, shapes, ontology, limits)
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


def _frame(cap: int) -> bytes | None:
    """Read a length-prefixed request without allocating beyond its bound."""
    import struct

    header = sys.stdin.buffer.read(8)
    if not header:
        return None
    if len(header) != 8:
        raise InputError("Truncated SHACL frame header")
    size = struct.unpack(">Q", header)[0]
    if size > cap:
        raise LimitExceeded("SHACL frame exceeds protocol byte bound")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        raise InputError("Truncated SHACL frame")
    return payload


def _respond(response: dict[str, Any], cap: int) -> None:
    """Emit a single bounded frame; no progress text enters the protocol."""
    import struct

    encoded = json.dumps(response, ensure_ascii=True).encode()
    if len(encoded) > cap:
        encoded = json.dumps(
            {
                "status": "exhausted",
                "message": "SHACL response exceeds protocol byte bound",
            }
        ).encode()
    sys.stdout.buffer.write(struct.pack(">Q", len(encoded)) + encoded)
    sys.stdout.buffer.flush()


def _prepared_job(
    plan: dict[str, Any],
    static: Dataset,
    anchors: dict[Any, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    """Release all dataset/function execution state when this independent job ends."""
    job = plan | request
    incoming = _roles(job)
    aliases: dict[Node, Node] = {}
    for anchor, node in incoming.graph("urn:bijux:shacl:anchors").subject_objects(
        ANCHOR["term"]
    ):
        if anchor not in anchors or not isinstance(node, BNode):
            raise InputError("Unknown SHACL static blank binding")
        aliases[node] = anchors[anchor]
    dataset = Dataset()
    budget = Budget(Limits(**plan["limits"]))
    for name in (
        "shapes",
        "authored-shapes",
        "ontology",
        "functions",
        "bindings",
    ):
        target = dataset.graph(URIRef(f"urn:bijux:shacl:{name}"))
        for triple in static.graph(URIRef(f"urn:bijux:shacl:{name}")):
            budget.consume("work")
            target.add(triple)
    for name in ("data", "bindings"):
        target = dataset.graph(URIRef(f"urn:bijux:shacl:{name}"))
        for subject, predicate, value in incoming.graph(
            URIRef(f"urn:bijux:shacl:{name}")
        ):
            budget.consume("work")
            if name == "bindings" and predicate != SELECTION.focus:
                raise InputError("SHACL batch cannot replace authored shape bindings")
            target.add(
                (
                    aliases.get(subject, subject),
                    predicate,
                    aliases.get(value, value),
                )
            )
    return execute(job, supplied=dataset, prepared=True)


def _session(cap: int) -> int:
    """Prepare one fixed contract, then execute fresh datasets until EOF or failure."""
    plan: dict[str, Any] | None = None
    static: Dataset | None = None
    anchors: dict[Any, Any] = {}
    while (payload := _frame(cap)) is not None:
        try:
            request = decode_json(payload, max_bytes=cap)
            if not isinstance(request, dict):
                raise InputError("Expected SHACL session request object")
            command = request.get("command")
            if command == "prepare" and plan is None:
                memory = request.get("memory_bytes")
                if memory is not None:
                    if type(memory) is not int or memory <= 0:
                        raise InputError("memory_bytes must be a positive integer")
                    try:
                        import resource

                        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
                    except (ImportError, AttributeError, OSError, ValueError) as error:
                        raise InputError(
                            "Requested worker memory bound is unavailable"
                        ) from error
                import pyshacl

                static = _roles(request)
                _, roots = read_bindings(static)
                problem = _prepare_shapes(
                    request,
                    static.graph("urn:bijux:shacl:authored-shapes"),
                    static.graph("urn:bijux:shacl:shapes"),
                    roots,
                    f"pyshacl {pyshacl.__version__}",
                )
                if problem is not None:
                    _respond(problem, cap)
                    return 0
                anchors = dict(
                    static.graph("urn:bijux:shacl:anchors").subject_objects(
                        ANCHOR["term"]
                    )
                )
                plan = {key: value for key, value in request.items() if key != "graphs"}
                _respond({"status": "ready"}, cap)
            elif command == "validate" and plan is not None and static is not None:
                if set(request) - {
                    "command",
                    "graphs",
                    "focus_nodes",
                    "binding_count",
                    "anchor_count",
                }:
                    raise InputError(
                        "SHACL batch cannot replace fixed preparation options"
                    )
                _respond(_prepared_job(plan, static, anchors, request), cap)
            else:
                raise InputError("Expected SHACL preparation or validation command")
        except Exception as error:
            status = "exception"
            if isinstance(error, (LimitExceeded, MemoryError)):
                status = "exhausted"
            elif isinstance(error, InputError):
                status = "input_error"
            elif isinstance(error, ImportError):
                status = "unavailable"
            else:
                from pyshacl.errors import (
                    ConstraintLoadError,
                    ReportableRuntimeError,
                    ShapeLoadError,
                )

                if isinstance(error, (ShapeLoadError, ConstraintLoadError)):
                    status = "shapes_invalid"
                elif isinstance(error, ReportableRuntimeError):
                    status = (
                        "shapes_invalid"
                        if str(error).startswith("SHACL File does not validate")
                        else "exhausted"
                        if str(error).startswith("Validation path too deep!\n")
                        else "failure"
                    )
            _respond(
                {
                    "status": status,
                    "type": type(error).__name__,
                    "message": str(error)[:2000],
                },
                cap,
            )
            return 1
        del request
        payload = b""
    return 0


def main() -> int:
    """Read one request and emit one size-bounded response without progress text."""
    try:
        # Parent and child independently enforce the private transport bound.
        cap = int(sys.argv[1])
        if cap <= 0:
            raise InputError("Invalid protocol byte bound")
        if "--session" in sys.argv[2:]:
            return _session(cap)
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
