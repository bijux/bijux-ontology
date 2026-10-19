"""Disposable RDFC-1.0 execution with explicit RDF 1.1 term and memory scope."""

import json
import sys
from typing import cast

import pyoxigraph as ox

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import decode_json


def _memory_limit(memory: object) -> None:
    """Request an actual OS address-space allowance or report its absence."""
    if memory is None:
        return
    if type(memory) is not int or memory <= 0:
        raise InputError("memory_bytes must be a positive integer")
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except (ImportError, AttributeError, OSError, ValueError) as error:
        raise ResourceError("Address-space memory limits unavailable") from error


def _term(value: object) -> ox.NamedNode | ox.BlankNode | ox.Literal | ox.DefaultGraph:
    """Convert exact tagged fields; never invoke RDF literal value conversion."""
    if not isinstance(value, list) or not value:
        raise InputError("Malformed canonical RDF term")
    if value == ["default"]:
        return ox.DefaultGraph()
    if len(value) == 2 and isinstance(value[1], str):
        if value[0] == "iri":
            return ox.NamedNode(value[1])
        if value[0] == "blank":
            return ox.BlankNode(value[1])
    if (
        len(value) == 4
        and value[0] == "literal"
        and isinstance(value[1], str)
        and (value[2] is None or isinstance(value[2], str))
        and isinstance(value[3], str)
    ):
        if value[2] is not None:
            if value[3] != "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString":
                raise InputError("Language literals require rdf:langString")
            return ox.Literal(value[1], language=value[2])
        return ox.Literal(value[1], datatype=ox.NamedNode(value[3]))
    raise InputError("Unsupported canonical RDF term")


def execute(request: object) -> dict[str, object]:
    """Canonicalize a bounded, immutable quad set using the maintained backend."""
    if not isinstance(request, dict):
        raise InputError("Expected a canonical RDF request object")
    supplied = request.get("limits")
    if not isinstance(supplied, dict) or any(
        not isinstance(name, str) or type(value) is not int
        for name, value in supplied.items()
    ):
        raise InputError("Expected canonical RDF integer limits")
    limits = Limits(**cast(dict[str, int], supplied))
    budget = Budget(limits)
    _memory_limit(request.get("memory_bytes"))
    algorithm = request.get("hash_algorithm")
    if algorithm not in ("SHA256", "SHA384"):
        raise InputError("Canonical RDF requires SHA256 or SHA384")
    records = request.get("quads")
    if not isinstance(records, list):
        raise InputError("Expected canonical RDF quad records")
    dataset = ox.Dataset()
    for record in records:
        budget.consume("triples")
        budget.consume("work")
        if not isinstance(record, list) or len(record) != 4:
            raise InputError("Malformed canonical RDF quad record")
        subject, predicate, value, graph = map(_term, record)
        if (
            not isinstance(subject, ox.NamedNode | ox.BlankNode)
            or not isinstance(predicate, ox.NamedNode)
            or not isinstance(value, ox.NamedNode | ox.BlankNode | ox.Literal)
            or not isinstance(graph, ox.NamedNode | ox.BlankNode | ox.DefaultGraph)
        ):
            raise InputError("Unsupported RDF 1.1 quad roles")
        dataset.add(ox.Quad(subject, predicate, value, graph))
    dataset.canonicalize(
        ox.CanonicalizationAlgorithm.RDFC_1_0_SHA_384
        if algorithm == "SHA384"
        else ox.CanonicalizationAlgorithm.RDFC_1_0
    )
    lines = []
    size = 0
    for quad in dataset:
        budget.consume("work")
        line = ox.serialize((quad,), format=ox.RdfFormat.N_QUADS)
        if line is None:
            raise InputError("Canonical RDF serializer returned no bytes")
        size += len(line)
        if size > limits.max_bytes:
            raise LimitExceeded("Canonical RDF artifact exceeds max_bytes")
        budget.consume("bytes", len(line))
        lines.append(line)
    content = b"".join(sorted(lines))
    return {
        "status": "result",
        "algorithm": "RDFC-1.0",
        "hash_algorithm": algorithm,
        "rdf_version": "1.1",
        "backend": f"pyoxigraph/{ox.__version__}",
        "quad_count": len(dataset),
        "content": content.decode("utf-8"),
    }


def main() -> int:
    """Read only the caller's bounded request and return one bounded envelope."""
    try:
        cap = int(sys.argv[1])
        if cap <= 0:
            return 2
        request = decode_json(sys.stdin.buffer.read(cap + 1), max_bytes=cap)
        try:
            result = execute(request)
        except ResourceError as error:
            result = {"status": "unavailable", "message": str(error)}
        except (LimitExceeded, MemoryError) as error:
            result = {
                "status": "exhausted",
                "message": str(error) or "Memory exhausted",
            }
        except (InputError, ValueError, TypeError, UnicodeError) as error:
            result = {"status": "input_error", "message": str(error)}
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > cap:
            encoded = (
                b'{"status":"exhausted",'
                b'"message":"Canonical output exceeds protocol bound"}'
            )
        if len(encoded) > cap:
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    except (IndexError, ValueError, InputError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
