"""One pinned native serializer/reparser, never a second RDF storage engine."""

import json
import sys
from importlib.metadata import version
from typing import BinaryIO, cast

import pyoxigraph as ox

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.triple_terms.protocol import decode_term, encode_term

from .types import REIFIER_CONTRACTS


class _Destination:
    """Prepay every maintained serializer write before storing its bytes."""

    def __init__(self, budget: Budget) -> None:
        """Own one bounded in-memory serialization destination."""
        self.budget = budget
        self.content = bytearray()

    def write(self, chunk: bytes) -> int:
        """Withhold output that exceeds either scalar or cumulative allowance."""
        if len(self.content) + len(chunk) > self.budget.limits.max_bytes:
            raise LimitExceeded("Reifier export exceeds max_bytes")
        self.budget.consume("work", len(chunk) + 1)
        self.budget.consume("bytes", len(chunk))
        self.content.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        """There is no external file to flush."""


def _evaluate(request: dict[str, object], budget: Budget) -> bytes:
    """Prove exact same-capture quad identity through maintained N-Quads 1.2."""
    if request.get("contract") not in REIFIER_CONTRACTS or cast(
        str, request["contract"]
    ).rsplit("-pyoxigraph-", 1)[-1] != version("pyoxigraph"):
        raise InputError("The selected pinned reifier backend profile is unavailable")
    values = request.get("quads")
    if not isinstance(values, list):
        raise InputError("Reifier serialization requires a finite quad inventory")
    rows = []
    blanks: dict[str, ox.BlankNode] = {}
    for row in values:
        budget.consume("work")
        budget.consume("triples")
        budget.consume("states")
        budget.consume("bytes", 64)
        if not isinstance(row, list) or len(row) != 4:
            raise InputError("Invalid reifier quad transport")
        s, p, o = (decode_term(value, blanks, budget) for value in row[:3])
        g = decode_term(row[3], blanks, budget) if row[3] is not None else None
        if (
            not isinstance(s, ox.NamedNode | ox.BlankNode)
            or not isinstance(p, ox.NamedNode)
            or g is not None
            and not isinstance(g, ox.NamedNode | ox.BlankNode)
        ):
            raise InputError("Invalid reifier quad term positions")
        rows.append(ox.Quad(s, p, o, g or ox.DefaultGraph()))
    destination = _Destination(budget)
    ox.serialize(rows, cast(BinaryIO, destination), ox.RdfFormat.N_QUADS)
    budget.consume("bytes", len(destination.content))
    content = bytes(destination.content)
    reparsed = []
    iterator = iter(ox.parse(content, ox.RdfFormat.N_QUADS, rename_blank_nodes=False))
    while True:
        budget.consume("work")
        try:
            quad = next(iterator)
        except StopIteration:
            break
        budget.consume("triples")
        budget.consume("states")
        budget.consume("bytes", 64)
        for term in (quad.subject, quad.predicate, quad.object):
            encode_term(term, budget)
        if not isinstance(quad.graph_name, ox.DefaultGraph):
            encode_term(quad.graph_name, budget)
        reparsed.append(quad)
    budget.consume("work", len(rows) + len(reparsed))
    budget.consume("states", len(rows) + len(reparsed))
    budget.consume("bytes", 128 * (len(rows) + len(reparsed)))
    if set(rows) != set(reparsed):
        raise InputError("Native serialization changed exact reifier quad identity")
    return content


def process_request(payload: bytes, cap: int) -> bytes:
    """Return a finite result with all six owned control counters."""
    budget = None
    reply: dict[str, object] = {
        "protocol": 1,
        "status": "unverified",
        "content": None,
        "message": "",
        "controls": None,
    }
    try:
        request = object_value(decode_json(payload, max_bytes=cap), location="reifiers")
        budget = ControlBudget(
            Limits(**cast(dict[str, int], request["limits"])), request["allowances"]
        )
        budget.consume("bytes", json_bound(request, budget) * 2)
        content = _evaluate(request, budget)
        budget.consume("bytes", len(content) * 2)
        budget.consume("work", len(content) * 2 + 1)
        reply.update(status="result", content=content.hex())
        budget.consume("bytes", json_bound(reply, budget) * 2)
    except Exception as error:
        reply.update(
            status="exhausted"
            if isinstance(error, LimitExceeded | MemoryError | RecursionError)
            else "unverified",
            content=None,
            message=str(error)[:1024] or type(error).__name__,
        )
    reply["controls"] = budget.consumed() if budget is not None else None
    raw = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode()
    return raw if len(raw) <= cap else b"{}"


def main() -> int:
    """Use only coordinator-owned bounded process transport."""
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
