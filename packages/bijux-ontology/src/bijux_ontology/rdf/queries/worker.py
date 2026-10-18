"""Maintained ASK/SELECT parsing and evaluation over supplied local RDF only."""

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any

import rdflib
from rdflib import BNode, Dataset, URIRef
from rdflib.plugins import sparql
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.processor import SPARQLProcessor

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.queries.literals import (
    LiteralScopeUnsupported,
    check_literal_scope,
)
from bijux_ontology.rdf.queries.protocol import read_term, term_data
from bijux_ontology.rdf.queries.syntax import check_query_syntax
from bijux_ontology.rdf.terms import validate_triple


def _checked_query(text: str, namespaces: Mapping[str, URIRef], budget: Budget) -> Any:
    """Reject retrieval in every maintained parse-tree branch before evaluation."""
    budget.consume("work", len(text))
    try:
        parsed = parseQuery(text)
        if parsed[1].name not in {"AskQuery", "SelectQuery"}:
            raise InputError("Local queries support ASK and SELECT only")
        pending: list[tuple[object, int]] = [(parsed, 0)]
        while pending:
            value, depth = pending.pop()
            budget.consume("work")
            budget.consume("states")
            if depth > budget.limits.max_depth:
                raise LimitExceeded("SPARQL query exceeds max_depth")
            if isinstance(value, CompValue):
                if value.name in {"ServiceGraphPattern", "DatasetClause"}:
                    raise InputError(
                        "Local queries disallow SERVICE and FROM/FROM NAMED"
                    )
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                pending.extend((child, depth + 1) for child in value)
        unavailable = None
        try:
            check_query_syntax(text, namespaces, budget)
        except ResourceError as error:
            # A parsed query may use a native unsupported function. Retain the
            # more precise literal-construction boundary when it also applies.
            unavailable = error
        translated = translateQuery(parsed, initNs=namespaces)
        check_literal_scope(translated.algebra, budget)
        if unavailable is not None:
            raise unavailable
        return translated
    except (InputError, LimitExceeded):
        raise
    except RecursionError as error:
        raise LimitExceeded("SPARQL parser exceeded its nesting allowance") from error
    except Exception as error:
        raise InputError(f"Cannot parse local SPARQL query: {error}") from error


def evaluate(request: dict[str, Any], cap: int) -> dict[str, Any]:
    """Use the maintained processor with exact literals and finite output retention."""
    rdflib.NORMALIZE_LITERALS = False
    sparql.SPARQL_DEFAULT_GRAPH_UNION = False
    sparql.SPARQL_LOAD_GRAPHS = False
    limits = Limits(**request["limits"])
    budget = Budget(limits)
    text = request["query"]
    namespaces = {prefix: URIRef(iri) for prefix, iri in request["namespaces"]}
    query = _checked_query(text, namespaces, budget)
    graph = Dataset(default_union=False)
    blanks: dict[str, BNode] = {}
    for name, triples in request["graphs"]:
        budget.consume("resources")
        node = read_term(name, blanks)
        if node is not None and not isinstance(node, (URIRef, BNode)):
            raise InputError("Invalid query dataset graph name")
        target = (
            graph.default_graph
            if node is None or not request["dataset"]
            else graph.graph(node)
        )
        for row in triples:
            budget.consume("triples")
            budget.consume("work")
            subject, predicate, value = (read_term(term, blanks) for term in row)
            if subject is None or predicate is None or value is None:
                raise InputError("Unbound terms are not RDF input triples")
            validate_triple(subject, predicate, value)
            target.add((subject, predicate, value))
    result = SPARQLProcessor(graph).query(query)  # type: ignore[no-untyped-call]
    if result["type_"] == "ASK":
        return {
            "status": "result",
            "kind": "ASK",
            "answer": bool(result["askAnswer"]),
            "complete": True,
        }
    variables = tuple(result["vars_"])
    budget.consume("states", len(variables))
    rows: list[list[dict[str, object] | None]] = []
    message = ""
    complete = True
    cap = min(cap, request["output_cap"])
    envelope = {
        "status": "result",
        "kind": "SELECT",
        "variables": [str(v) for v in variables],
        "rows": [],
        "complete": False,
        "message": "SELECT output exceeds protocol byte bound",
    }
    output_size = len(
        json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode()
    )
    # Public processor bindings preserve completely unbound solutions, which the
    # RDFLib Result iterator can discard as empty mappings.
    try:
        for binding in result["bindings"]:
            budget.consume("work")
            if len(rows) >= request["max_rows"]:
                raise LimitExceeded("SELECT result exceeds max_rows")
            budget.consume("states", len(variables))
            row = [term_data(binding.get(variable)) for variable in variables]
            encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()
            if len(encoded) > limits.max_bytes:
                raise LimitExceeded("SELECT row exceeds max_bytes")
            budget.consume("bytes", len(encoded))
            retained_size = output_size + len(encoded) + int(bool(rows))
            if retained_size > cap:
                raise LimitExceeded("SELECT output exceeds protocol byte bound")
            output_size = retained_size
            rows.append(row)
    except LimitExceeded as error:
        complete, message = False, str(error)
    return {
        "status": "result",
        "kind": "SELECT",
        "variables": [str(v) for v in variables],
        "rows": rows,
        "complete": complete,
        "message": message,
    }


def process_request(payload: bytes, cap: int) -> bytes:
    """Execute the same bounded protocol inside an already owned worker."""
    previous = (
        rdflib.NORMALIZE_LITERALS,
        sparql.SPARQL_DEFAULT_GRAPH_UNION,
        sparql.SPARQL_LOAD_GRAPHS,
    )
    try:
        if len(payload) > cap:
            raise LimitExceeded("Local query request exceeds protocol byte bound")
        request = decode_json(payload, max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Expected a local query request object")
        response = evaluate(request, cap)
    except Exception as error:  # noqa: BLE001 - worker boundary returns typed failure
        response = {
            "status": "exhausted"
            if isinstance(error, (LimitExceeded, MemoryError))
            else "unverified"
            if isinstance(error, LiteralScopeUnsupported)
            else "unavailable"
            if isinstance(error, ResourceError)
            else "input_error",
            "message": f"{type(error).__name__}: {str(error)[:2000]}",
        }
    finally:
        (
            rdflib.NORMALIZE_LITERALS,
            sparql.SPARQL_DEFAULT_GRAPH_UNION,
            sparql.SPARQL_LOAD_GRAPHS,
        ) = previous
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > cap:
        encoded = json.dumps(
            {
                "status": "exhausted",
                "message": "Local query response exceeds protocol byte bound",
            }
        ).encode()
    return encoded


def main() -> int:
    """Accept one bounded JSON request; emit one bounded structured result."""
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
