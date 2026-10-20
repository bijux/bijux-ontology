"""One fixed owned native answer parser; no query execution or retrieval."""

import json
import sys
from collections.abc import Mapping
from typing import IO, cast

import pyoxigraph as ox
from rdflib import BNode

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.queries.protocol import QueryTerm, read_term, term_data
from bijux_ontology.rdf.terms import oxigraph_term

from .types import AnswerUnavailable
from .xml import read_xml


def _standard(value: object, budget: Budget) -> dict[str, object]:
    """Adapt strict standard fields through the existing exact term constructor."""
    root = object_value(value, location="answer")
    if "head" not in root or ("boolean" in root) == ("results" in root):
        raise InputError("Answer requires head and either boolean or results")
    # W3C permits additional root keys. Bounded non-answer metadata does not
    # participate in typed row identity or establish external completeness.
    head = object_value(root["head"], location="head")
    if set(head) - {"vars", "link"}:
        raise InputError("Unsupported SPARQL answer header fields")
    if "link" in head:
        links = head["link"]
        if not isinstance(links, list) or any(type(link) is not str for link in links):
            raise InputError("Answer links require strings")
        for link in links:
            bound_text(link, budget)
    if "boolean" in root:
        if type(root["boolean"]) is not bool or "vars" in head:
            raise InputError("ASK requires a boolean and no SELECT variables")
        return {"kind": "ASK", "answer": root["boolean"], "variables": [], "rows": []}
    variables = head.get("vars")
    if not isinstance(variables, list) or any(type(v) is not str for v in variables):
        raise InputError("SELECT requires declared variables")
    known: set[str] = set()
    for v in variables:
        bound_text(v, budget)
        budget.consume("states")
        ox.Variable(v)
        if v in known:
            raise InputError("Duplicate answer variable")
        known.add(v)
    results = object_value(root["results"], location="results")
    if set(results) != {"bindings"} or not isinstance(results["bindings"], list):
        raise InputError("SELECT requires a bindings array")
    rows: list[list[object]] = []
    blanks: dict[str, BNode] = {}
    for raw in results["bindings"]:
        row = object_value(raw, location="binding")
        budget.consume("states", len(variables) + 1)
        budget.consume("bytes", 64 * (len(variables) + 1))
        if set(row) - known:
            raise InputError("Binding uses an undeclared variable")
        cells: list[object] = []
        for v in variables:
            budget.consume("work")
            if v not in row:
                cells.append(None)
                continue
            cell = object_value(row[v], location=v)
            kind = cell.get("type")
            text = cell.get("value")
            if type(text) is not str or type(kind) is not str:
                raise InputError("RDF answer cells require type and text")
            for key, item in cell.items():
                bound_text(key, budget)
                if type(item) is not str:
                    raise InputError("Answer term metadata must be strings")
                bound_text(item, budget)
            if kind == "literal":
                if set(cell) - {"type", "value", "datatype", "xml:lang"}:
                    raise InputError("Unsupported literal answer fields")
                data = {
                    "kind": "literal",
                    "value": text,
                    "datatype": cell.get("datatype"),
                    "language": cell.get("xml:lang"),
                }
            elif kind in {"uri", "bnode"} and set(cell) == {"type", "value"}:
                data = {"kind": "iri" if kind == "uri" else "blank", "value": text}
            else:
                raise InputError("Unsupported RDF answer term")
            cells.append(term_data(read_term(data, blanks)))
        rows.append(cells)
    return {"kind": "SELECT", "answer": None, "variables": variables, "rows": rows}


def parse_answer(payload: str, format: str, budget: Budget) -> dict[str, object]:
    """Use actual maintained syntax, with exact XML character-data capture."""
    bound_text(payload, budget)
    budget.consume("bytes", len(payload) * 48 + 1024)
    if format == "xml":
        return _standard(read_xml(payload, budget), budget)
    if format not in {"json", "tsv"}:
        raise InputError("Only JSON, XML and TSV carry exact answer artifacts")
    raw = None
    if format == "json":
        decoded = object_value(
            decode_json(
                payload,
                max_bytes=budget.limits.max_bytes,
                max_depth=budget.limits.max_depth,
            ),
            location="answer",
        )
        raw = _standard(decoded, budget)
        # The native reader rejects permitted non-answer root extensions. Its
        # input is the same strictly checked answer, without documentary fields.
        answer = {
            key: decoded[key]
            for key in ("head", "boolean" if "boolean" in decoded else "results")
        }
        budget.consume("bytes", 2 * json_bound(answer, budget))
        payload = json.dumps(answer, ensure_ascii=True, separators=(",", ":"))
    native = ox.parse_query_results(
        payload, format=getattr(ox.QueryResultsFormat, format.upper())
    )
    if isinstance(native, ox.QueryBoolean):
        if format != "json":
            raise InputError("TSV supports SELECT only")
        return cast(dict[str, object], raw)
    variables = [v.value for v in native.variables]
    rows = []
    blanks: dict[str, BNode] = {}
    for solution in native:
        budget.consume("work")
        budget.consume("states", len(variables) + 1)
        budget.consume("bytes", 128 * (len(variables) + 1))
        cells: list[object] = []
        for node in solution:
            budget.consume("work")
            if node is None:
                cells.append(None)
            else:
                term = oxigraph_term(node, blanks, True)
                for text in (
                    str(term),
                    str(getattr(term, "datatype", "") or ""),
                    str(getattr(term, "language", "") or ""),
                ):
                    bound_text(text, budget)
                cells.append(term_data(cast(QueryTerm, term)))
        rows.append(cells)
    if raw is not None:
        return raw
    return {"kind": "SELECT", "answer": None, "variables": variables, "rows": rows}


def process_request(payload: bytes) -> bytes:
    """Return a bounded typed answer and exact owned-control receipt."""
    envelope = object_value(
        decode_json(payload, max_bytes=len(payload) + 1), location="request"
    )
    limits = Limits(**cast(Mapping[str, int], envelope["limits"]))
    budget = ControlBudget(limits, envelope["allowances"])
    response: dict[str, object] = {"status": "ok", "data": None, "message": ""}
    try:
        budget.consume("bytes", 4096)
        budget.consume("work", 1024)
        budget.consume("states", 16)
        text, format = envelope["payload"], envelope["format"]
        if not isinstance(text, str) or not isinstance(format, str):
            raise InputError("Malformed answer request")
        if envelope["mode"] == "parse":
            response["data"] = parse_answer(text, format, budget)
        elif envelope["mode"] == "write":
            _standard(decode_json(text, max_bytes=limits.max_bytes), budget)
            native = ox.parse_query_results(text, format=ox.QueryResultsFormat.JSON)

            class Sink:
                """Refuse output chunks before retaining or joining their bytes."""

                def __init__(self) -> None:
                    """Own only chunks paid before their retained references."""
                    self.parts: list[bytes] = []
                    self.size = 0

                def flush(self) -> None:
                    """The bounded in-memory sink has no pending external output."""

                def write(self, chunk: bytes) -> int:
                    """Bound actual maintained serializer chunks before retention."""
                    budget.consume("work", len(chunk) + 1)
                    original_size = len(chunk)
                    carriage_returns = chunk.count(b"\r") if format == "xml" else 0
                    expanded_size = original_size + 4 * carriage_returns
                    if self.size + expanded_size > limits.max_bytes:
                        raise LimitExceeded("Serialized answer exceeds max_bytes")
                    budget.consume("bytes", expanded_size * 5 + 64)
                    budget.consume("states")
                    # Validated identifiers/variables cannot contain CR. On
                    # native XML output it is literal content, whose exact RDF
                    # spelling requires a reference before XML end-line folding.
                    if carriage_returns:
                        chunk = chunk.replace(b"\r", b"&#13;")
                    self.parts.append(chunk)
                    self.size += expanded_size
                    return original_size

            sink = Sink()
            native.serialize(
                output=cast(IO[bytes], sink),
                format=getattr(ox.QueryResultsFormat, format.upper()),
            )
            budget.consume("bytes", sink.size * 8)
            serialized_answer = b"".join(sink.parts).decode("utf-8")
            if format == "xml":
                try:
                    read_xml(serialized_answer, budget)
                except LimitExceeded:
                    raise
                except InputError as error:
                    raise AnswerUnavailable(
                        "XML 1.0 cannot represent this valid RDF answer: " + str(error)
                    ) from error
            response["data"] = {"serialized": serialized_answer}
        else:
            raise InputError("Unsupported fixed answer codec operation")
        size = json_bound(response, budget) + 512
        budget.consume("bytes", 2 * size)
        budget.consume("work", size)
    except (InputError, ValueError, SyntaxError) as error:
        response.update(
            status=(
                "exhausted"
                if isinstance(error, LimitExceeded)
                else "unverified"
                if isinstance(error, AnswerUnavailable)
                else "input_error"
            ),
            data=None,
            message=str(error)[:1024],
        )
    response["controls"] = budget.consumed()
    raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(raw) > cast(int, envelope["cap"]):
        raise LimitExceeded("Answer response exceeds reserved output capacity")
    return raw


def main() -> None:
    """Read a single bounded host-owned envelope and emit its sole response."""
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read()))


if __name__ == "__main__":
    main()
