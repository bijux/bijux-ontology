"""Private bounded JSON transport and owned SHACL worker session lifetime."""

import json
import sys
from typing import Any

from rdflib import BNode, Dataset, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import decode_json
from bijux_ontology.shacl.bindings import ANCHOR, SELECTION, read_bindings
from bijux_ontology.shacl.execution.backend import execute, prepare_shapes, read_roles


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
    incoming = read_roles(job)
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

                static = read_roles(request)
                _, roots = read_bindings(static)
                problem = prepare_shapes(
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
