"""Bounded native SPARQL evaluation over supplied local RDF."""

import json
from typing import Any

import rdflib
from rdflib import BNode, Dataset, URIRef
from rdflib.plugins import sparql
from rdflib.plugins.sparql.processor import SPARQLProcessor

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import decode_json
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import read_term, term_data
from bijux_ontology.rdf.queries.syntax import prepare_local_query
from bijux_ontology.rdf.terms import validate_triple


def evaluate(request: dict[str, Any], cap: int) -> dict[str, Any]:
    """Use the maintained processor with exact literals and finite output retention."""
    rdflib.NORMALIZE_LITERALS = False
    sparql.SPARQL_DEFAULT_GRAPH_UNION = False
    sparql.SPARQL_LOAD_GRAPHS = False
    limits = Limits(**request["limits"])
    budget = Budget(limits)
    text = request["query"]
    namespaces = {prefix: URIRef(iri) for prefix, iri in request["namespaces"]}
    query = prepare_local_query(text, namespaces, budget, rdflib_execution=True)
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
