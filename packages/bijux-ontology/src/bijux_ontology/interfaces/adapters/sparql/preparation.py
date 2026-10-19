"""Closed typed SPARQL construction and exact result term decoding."""

import re
from typing import cast

from rdflib import BNode, Literal, URIRef

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node, literal_term, validate_triple

from .types import EndpointCompleteness, EndpointQuery

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_PARAMETER = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def capture_query(query: EndpointQuery, operation: Operation) -> EndpointQuery:
    """Own validated RDF parameters before constructing any query text."""
    if type(query) is not EndpointQuery:
        raise InputError("Endpoint extraction requires EndpointQuery")
    owned: list[URIRef | Literal | None] = []
    for name in ("subject", "predicate", "object", "graph"):
        value = getattr(query, name)
        if value is None:
            owned.append(None)
            continue
        if not isinstance(value, URIRef | Literal) or (
            name != "object" and not isinstance(value, URIRef)
        ):
            raise InputError("Endpoint parameters require IRIs or an object literal")
        bound_node(value, operation)
        owned.append(
            literal_term(
                str(value),
                datatype=str(value.datatype) if value.datatype else None,
                language=value.language,
            )
            if isinstance(value, Literal)
            else URIRef(str(value))
        )
    operation.consume("bytes", 512)
    return EndpointQuery(
        cast(URIRef | None, owned[0]),
        cast(URIRef | None, owned[1]),
        owned[2],
        cast(URIRef | None, owned[3]),
    )


def capture_contract(
    contract: EndpointCompleteness | None, operation: Operation
) -> EndpointCompleteness | None:
    """Reject ambiguous parameter/header declarations before opening a socket."""
    if contract is None:
        return None
    if type(contract) is not EndpointCompleteness:
        raise InputError("Endpoint completeness requires a typed contract")
    for value in (
        contract.snapshot_parameter,
        contract.snapshot,
        contract.snapshot_header,
        contract.total_header,
        contract.truncated_header,
    ):
        if type(value) is not str or not value:
            raise InputError("Endpoint contract fields require nonempty plain text")
        bound_text(value, operation)
    if not _PARAMETER.fullmatch(contract.snapshot_parameter) or (
        contract.snapshot_parameter.lower()
        in {"query", "format", "default-graph-uri", "named-graph-uri"}
    ):
        raise InputError("Snapshot parameter conflicts with the SPARQL protocol")
    headers = (
        contract.snapshot_header,
        contract.total_header,
        contract.truncated_header,
    )
    if (
        any(not _TOKEN.fullmatch(value) for value in headers)
        or len({value.lower() for value in headers}) != 3
    ):
        raise InputError("Endpoint receipt header names must be distinct HTTP tokens")
    if not contract.snapshot.isascii() or any(
        ord(char) < 33 or ord(char) == 127 for char in contract.snapshot
    ):
        raise InputError("Snapshot receipt requires visible ASCII text")
    return EndpointCompleteness(
        contract.snapshot_parameter, contract.snapshot, *headers
    )


def query_text(query: EndpointQuery, page_size: int, offset: int) -> str:
    """Construct one bounded, deterministic, distinct ground-triple window."""
    pattern = "?s ?p ?o ."
    if query.graph is not None:
        pattern = "GRAPH " + query.graph.n3() + " { " + pattern + " }"
    values = " ".join(
        "VALUES ?" + name + " { " + value.n3() + " }"
        for name, value in (
            ("s", query.subject),
            ("p", query.predicate),
            ("o", query.object),
        )
        if value is not None
    )
    return (
        "SELECT DISTINCT ?s ?p ?o WHERE { " + pattern + " " + values + " } "
        "ORDER BY (STR(?s)) (STR(?p)) (STR(?o)) (isIRI(?o)) "
        '(COALESCE(LANG(?o), "")) (COALESCE(STR(DATATYPE(?o)), "")) LIMIT '
        + str(page_size)
        + " OFFSET "
        + str(offset)
    )


def read_rows(
    payload: bytes, operation: Operation
) -> list[tuple[URIRef | BNode, URIRef, URIRef | BNode | Literal]]:
    """Preserve SPARQL JSON lexical forms and one response's blank-node scope."""
    operation.consume("bytes", 64 * len(payload) + 1024)
    data = object_value(
        decode_json(
            payload,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        ),
        location="endpoint",
    )
    if set(data) != {"head", "results"}:
        raise InputError("Endpoint requires SELECT SPARQL JSON results")
    head = object_value(data["head"], location="endpoint head")
    results = object_value(data["results"], location="endpoint results")
    if (
        set(head) - {"vars", "link"}
        or head.get("vars") != ["s", "p", "o"]
        or set(results) != {"bindings"}
    ):
        raise InputError("Endpoint result projection differs from the selected triples")
    rows = results["bindings"]
    if not isinstance(rows, list):
        raise InputError("Endpoint bindings require an array")
    blanks: dict[str, BNode] = {}
    output: list[tuple[URIRef | BNode, URIRef, URIRef | BNode | Literal]] = []
    for row in rows:
        operation.consume("states")
        values = object_value(row, location="endpoint row")
        if set(values) != {"s", "p", "o"}:
            raise InputError("Endpoint triple rows require all three bound terms")
        nodes: list[URIRef | BNode | Literal] = []
        node: URIRef | BNode | Literal
        for key in ("s", "p", "o"):
            operation.consume("work")
            term = object_value(values[key], location="endpoint term")
            kind, lexical = term.get("type"), term.get("value")
            if type(lexical) is not str:
                raise InputError("Endpoint RDF terms require lexical text")
            bound_text(lexical, operation)
            if kind == "uri" and set(term) == {"type", "value"}:
                node = URIRef(absolute_iri(lexical))
            elif kind == "bnode" and set(term) == {"type", "value"} and lexical:
                node = blanks.setdefault(lexical, BNode())
            elif kind == "literal" and set(term) <= {
                "type",
                "value",
                "datatype",
                "xml:lang",
            }:
                datatype, language = term.get("datatype"), term.get("xml:lang")
                if datatype is not None:
                    if type(datatype) is not str:
                        raise InputError("Endpoint datatypes require IRIs")
                    bound_text(datatype, operation)
                if language is not None:
                    if type(language) is not str:
                        raise InputError("Endpoint languages require text")
                    bound_text(language, operation)
                node = literal_term(lexical, datatype=datatype, language=language)
            else:
                raise InputError("Unsupported SPARQL JSON term")
            bound_node(node, operation)
            nodes.append(node)
        subject, predicate, object_ = nodes
        validate_triple(subject, predicate, object_)
        assert isinstance(subject, URIRef | BNode) and isinstance(predicate, URIRef)
        operation.consume("triples")
        output.append((subject, predicate, object_))
    return output


def receipt(
    headers: tuple[tuple[str, str], ...],
    contract: EndpointCompleteness,
) -> tuple[str, int]:
    """Require unique native server receipts, never infer completeness by paging."""

    def value(name: str) -> str:
        """Require exactly one case-insensitive native response header."""
        selected = [v for k, v in headers if k.lower() == name.lower()]
        if len(selected) != 1:
            raise InputError("Endpoint omitted a unique required snapshot receipt")
        return selected[0]

    snapshot, total = value(contract.snapshot_header), value(contract.total_header)
    if snapshot != contract.snapshot or value(contract.truncated_header) != "false":
        raise InputError("Endpoint snapshot changed or response was truncated")
    if not total.isascii() or not total.isdigit() or len(total) > 19:
        raise InputError("Endpoint total row receipt requires a bounded integer")
    return snapshot, int(total)
