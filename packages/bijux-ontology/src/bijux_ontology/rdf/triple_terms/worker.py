"""Owned maintained RDF 1.2 parsing and exact graph-only serialization."""

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, BinaryIO, cast

import pyoxigraph as ox

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.loading import _read

from .protocol import decode_quads, encode_quads, encode_term
from .types import TRIPLE_TERM_CONTRACT


class _Destination:
    """Bound maintained serializer writes before retaining output chunks."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.content = bytearray()

    def write(self, chunk: bytes) -> int:
        """Reserve exact bytes before extending the owned output."""
        self.budget.consume("work")
        if len(self.content) + len(chunk) > self.budget.limits.max_bytes:
            raise LimitExceeded("Triple-term export exceeds max_bytes")
        self.budget.consume("bytes", len(chunk))
        self.content.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        """The serializer owns no external output resource."""


def _parsed(
    payload: bytes,
    format: ox.RdfFormat,
    base: str | None,
    budget: Budget,
    *,
    rename: bool,
) -> tuple[ox.Quad, ...]:
    """Collect maintained parser output only after per-record reservations."""
    iterator = iter(ox.parse(payload, format, base_iri=base, rename_blank_nodes=rename))
    rows = []
    while True:
        budget.consume("work")
        try:
            quad = next(iterator)
        except StopIteration:
            break
        budget.consume("triples")
        budget.consume("states")
        budget.consume("bytes", 64)
        if not isinstance(quad.graph_name, ox.DefaultGraph):
            raise InputError("Named graphs are outside this triple-term profile")
        for term in (quad.subject, quad.predicate, quad.object):
            encode_term(term, budget)
        rows.append(quad)
    budget.consume("states", len(rows))
    budget.consume("bytes", 32 + 8 * len(rows))
    return tuple(rows)


def evaluate(request: dict[str, Any]) -> dict[str, object]:
    """Execute one explicit pinned backend contract with cumulative allowances."""
    budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
    try:
        if (
            request.get("contract") != TRIPLE_TERM_CONTRACT
            or version("pyoxigraph") != "0.5.11"
        ):
            raise InputError("The selected triple-term backend contract is unavailable")
        if request["action"] == "parse":
            content = request["content"]
            if not isinstance(content, str):
                raise InputError("Invalid triple-term source transport")
            kind = request["kind"]
            source: Path | bytes | str
            if kind == "path":
                bound_text(content, budget)
                source = Path(content)
            elif kind == "bytes":
                if len(content) > 2 * budget.limits.max_bytes:
                    raise LimitExceeded("Triple-term bytes exceed max_bytes")
                budget.consume("work", len(content) + 1)
                budget.consume("bytes", len(content) // 2)
                source = bytes.fromhex(content)
            elif kind == "text":
                source = content
            else:
                raise InputError("Unsupported triple-term source kind")
            budget.consume("resources")
            payload = _read(source, budget.limits.max_bytes, budget, binary=False)
            # Hash only already qualified exact source bytes.
            budget.consume("work", len(payload) + 1)
            digest = hashlib.sha256(payload).hexdigest()
            selected = {"turtle": ox.RdfFormat.TURTLE, "nt": ox.RdfFormat.N_TRIPLES}
            if request["format"] not in selected:
                raise InputError("Unsupported triple-term input syntax")
            base = request["base"]
            if base is not None:
                bound_text(base, budget)
            rows = _parsed(
                payload, selected[request["format"]], base, budget, rename=True
            )
            result: dict[str, object] = {
                "status": "result",
                "rows": encode_quads(rows, budget),
                "sha256": digest,
            }
        elif request["action"] == "export":
            rows = decode_quads(request["rows"], budget)
            destination = _Destination(budget)
            ox.serialize(rows, cast(BinaryIO, destination), ox.RdfFormat.N_TRIPLES)
            budget.consume("bytes", len(destination.content))
            payload = bytes(destination.content)
            reparsed = _parsed(
                payload, ox.RdfFormat.N_TRIPLES, None, budget, rename=False
            )
            # Same serialized blank tokens belong to this single capture; this
            # does not compare labels across independently supplied documents.
            for inventory in (rows, reparsed):
                budget.consume("work", len(inventory))
                budget.consume("states", len(inventory))
                budget.consume("bytes", 64 + 64 * len(inventory))
            if frozenset(rows) != frozenset(reparsed):
                raise InputError("Maintained export changed exact RDF terms")
            budget.consume("bytes", 2 * len(payload))
            result = {"status": "result", "content": payload.hex()}
        else:
            raise InputError("Unknown triple-term worker operation")
        budget.consume("bytes", json_bound(result, budget))
        budget.consume("bytes", json_bound({"consumed": request["allowances"]}, budget))
    except (LimitExceeded, RecursionError):
        result = {"status": "exhausted"}
    except SyntaxError:
        result = {"status": "input_error"}
    except (InputError, ValueError, TypeError, OSError):
        result = {"status": "unsupported"}
    result["consumed"] = budget.consumed()
    return result


def main() -> int:
    """Use the existing owned worker transport; never publish partial graphs."""
    try:
        cap = int(sys.argv[1])
        request = decode_json(sys.stdin.buffer.read(cap + 1), max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Invalid triple-term request")
        result = evaluate(request)
        payload = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
        if len(payload) > cap:
            payload = b'{"status":"exhausted"}'
        sys.stdout.buffer.write(payload)
    except (InputError, LimitExceeded, MemoryError, RecursionError):
        sys.stdout.buffer.write(b'{"status":"exhausted"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
