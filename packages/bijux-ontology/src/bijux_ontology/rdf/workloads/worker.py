"""Fixed owned adapters for maintained RDFLib and Pyoxigraph query execution."""

import json
import platform
import sys
from collections.abc import Iterable, Mapping
from importlib.metadata import version
from typing import cast

import pyoxigraph as ox
import rdflib
from rdflib import BNode, Dataset, Literal, URIRef, Variable
from rdflib.plugins import sparql
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.processor import SPARQLProcessor
from rdflib.plugins.sparql.sparql import Query

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.queries.protocol import QueryTerm, read_term, term_data
from bijux_ontology.rdf.queries.syntax import prepare_local_query
from bijux_ontology.rdf.terms import (
    bound_node,
    oxigraph_term,
    term_key,
    validate_triple,
)


class _Unsupported(InputError):
    """An explicit semantic configuration or query feature is unqualified."""


def _array(value: object) -> list[object]:
    """Keep native protocols finite without accepting iterator behavior."""
    if not isinstance(value, list):
        raise InputError("Expected a workload protocol array")
    return value


def _profile(query: Query, bindings: dict[str, QueryTerm], budget: Budget) -> None:
    """Inspect maintained algebra without creating another query representation."""
    pending: list[tuple[object, int]] = [(query.algebra, 0)]
    while pending:
        value, depth = pending.pop()
        budget.consume("work")
        budget.consume("states")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Workload algebra exceeds max_depth")
        if isinstance(value, CompValue):
            if value.name in {
                "Builtin_RAND",
                "Builtin_UUID",
                "Builtin_STRUUID",
                "Builtin_NOW",
                "Builtin_BNODE",
                "Aggregate_Sample",
                "Aggregate_GroupConcat",
                "Reduced",
                "Slice",
            }:
                raise _Unsupported(
                    "Nondeterministic functions, reductions and slices are unqualified"
                )
            if value.name == "Function":
                raise _Unsupported(
                    "Extension IRI functions and casts are outside the common profile"
                )
        children: Iterable[object]
        if isinstance(value, Mapping):
            children = value.values()
        elif isinstance(value, Iterable) and not isinstance(value, str | bytes):
            children = value
        else:
            continue
        for child in children:
            budget.consume("states")
            budget.consume("bytes", 24)
            pending.append((child, depth + 1))
    projected = (
        {str(item) for item in query.algebra["PV"]} if "PV" in query.algebra else set()
    )
    if bindings and (
        query.algebra.name != "SelectQuery" or not set(bindings) <= projected
    ):
        raise _Unsupported("Common native bindings require selected SELECT variables")


def _native(term: QueryTerm) -> ox.NamedNode | ox.BlankNode | ox.Literal:
    """Use maintained constructors; store fidelity is checked after insertion."""
    if isinstance(term, URIRef):
        return ox.NamedNode(str(term))
    if isinstance(term, BNode):
        return ox.BlankNode(str(term))
    return (
        ox.Literal(str(term), language=term.language)
        if term.language
        else ox.Literal(
            str(term),
            datatype=ox.NamedNode(str(term.datatype)) if term.datatype else None,
        )
    )


def _rows(
    result: object,
    variables: tuple[str, ...],
    budget: Budget,
    maximum: int,
    cap: int,
    *,
    native: bool,
) -> list[list[dict[str, object] | None]]:
    """Retain duplicate and fully unbound rows, stopping before truncation passes."""
    if not isinstance(result, Iterable):
        raise InputError("Native SELECT did not return iterable bindings")
    rows: list[list[dict[str, object] | None]] = []
    blanks: dict[str, BNode] = {}
    wire_size = 1024
    for binding in cast(Iterable[object], result):
        budget.consume("work")
        budget.consume("states", len(variables) + 1)
        budget.consume("bytes", 128 * (len(variables) + 1))
        if len(rows) >= maximum:
            raise LimitExceeded("Workload SELECT exceeds max_rows")
        row = []
        for name in variables:
            if native:
                value = cast(ox.QuerySolution, binding)[name]
                term = oxigraph_term(value, blanks, True) if value is not None else None
            else:
                if not isinstance(binding, Mapping):
                    raise InputError("RDFLib SELECT did not return a binding mapping")
                term = cast(Mapping[Variable, QueryTerm], binding).get(Variable(name))
            if term is not None:
                bound_node(term, budget)
            row.append(term_data(term))
        wire_size += json_bound(row, budget)
        if wire_size > cap:
            raise LimitExceeded("Workload SELECT exceeds its response cap")
        rows.append(row)
    return rows


def evaluate(request: dict[str, object], budget: Budget) -> dict[str, object]:
    """Run one declared local engine; no retrieval, inference or custom callbacks."""
    text = request["query"]
    config = object_value(request["config"], location="engine")
    if type(text) is not str or config["entailment"] != "asserted":
        raise _Unsupported("Only supplied queries over asserted RDF are qualified")
    rdflib.NORMALIZE_LITERALS = False
    sparql.SPARQL_LOAD_GRAPHS = False
    sparql.SPARQL_DEFAULT_GRAPH_UNION = config["default_graph"] == "union"
    namespaces = {}
    for pair in _array(request["namespaces"]):
        name, iri = _array(pair)
        if type(name) is not str or type(iri) is not str:
            raise InputError("Invalid workload namespace")
        bound_text(name, budget)
        bound_text(iri, budget)
        namespaces[name] = URIRef(iri)
    bindings: dict[str, QueryTerm] = {}
    blanks: dict[str, BNode] = {}
    for raw in _array(request["bindings"]):
        name, data = _array(raw)
        if type(name) is not str or name in bindings:
            raise InputError("Invalid workload binding name")
        ox.Variable(name)
        value = read_term(data, blanks)
        if not isinstance(value, URIRef | Literal):
            raise InputError("Workload bindings require IRI or literal terms")
        bound_node(value, budget)
        bindings[name] = value
    query = cast(Query, prepare_local_query(text, namespaces, budget))
    _profile(query, bindings, budget)
    dataset = Dataset(default_union=config["default_graph"] == "union")
    store = ox.Store() if config["engine"] == "pyoxigraph" else None
    expected: set[tuple[tuple[str | None, ...], ...]] = set()
    for raw in _array(request["graphs"]):
        name_data, supplied = _array(raw)
        budget.consume("resources")
        name = read_term(name_data, blanks)
        if name is not None and not isinstance(name, URIRef | BNode):
            raise InputError("Invalid workload graph name")
        target = dataset.default_graph if name is None else dataset.graph(name)
        native_name = ox.DefaultGraph() if name is None else _native(name)
        if isinstance(native_name, ox.Literal):
            raise InputError("Literal graph name is unsupported")
        if store is not None:
            store.add_graph(native_name)
        for raw_row in _array(supplied):
            budget.consume("work")
            budget.consume("states")
            budget.consume("triples")
            budget.consume("bytes", 256)
            row = _array(raw_row)
            if len(row) != 3:
                raise InputError("Invalid workload triple width")
            terms = [read_term(value, blanks) for value in row]
            if any(value is None for value in terms):
                raise InputError("RDF input cannot contain unbound terms")
            subject, predicate, value = cast(
                tuple[QueryTerm, QueryTerm, QueryTerm], tuple(terms)
            )
            for node in (subject, predicate, value):
                bound_node(node, budget)
            validate_triple(subject, predicate, value)
            target.add((subject, predicate, value))
            if store is not None:
                ns, np, nv = _native(subject), _native(predicate), _native(value)
                if not isinstance(ns, ox.NamedNode | ox.BlankNode) or not isinstance(
                    np, ox.NamedNode
                ):
                    raise InputError("Invalid native workload term role")
                store.add(ox.Quad(ns, np, nv, native_name))
                expected.add(
                    tuple(term_key(node) for node in (subject, predicate, value, name))
                )
    output_cap = request["output_cap"]
    if type(output_cap) is not int or output_cap <= 0:
        raise InputError("Invalid workload response cap")
    maximum = request["max_rows"]
    if type(maximum) is not int or maximum <= 0:
        raise InputError("Invalid workload row bound")
    if store is None:
        result = SPARQLProcessor(dataset).query(query, initBindings=bindings)  # type: ignore[no-untyped-call]
        if result["type_"] == "ASK":
            return {
                "status": "result",
                "kind": "ASK",
                "answer": bool(result["askAnswer"]),
                "complete": True,
            }
        variables = tuple(str(value) for value in result["vars_"])
        rows = _rows(
            result["bindings"], variables, budget, maximum, output_cap, native=False
        )
    else:
        actual = set()
        budget.consume("states", len(blanks))
        budget.consume("bytes", 64 * len(blanks))
        native_blanks = {str(node): node for node in blanks.values()}
        # Canonicalization occurs in Store.add, so constructor fidelity alone
        # cannot establish that this actual dataset plan retained exact terms.
        for quad in store:
            budget.consume("work")
            budget.consume("states")
            budget.consume("bytes", 256)
            native_terms = (quad.subject, quad.predicate, quad.object)
            nodes = tuple(
                oxigraph_term(value, native_blanks, True) for value in native_terms
            )
            graph = (
                None
                if isinstance(quad.graph_name, ox.DefaultGraph)
                else oxigraph_term(quad.graph_name, native_blanks, True)
            )
            actual.add(tuple(term_key(node) for node in (*nodes, graph)))
        if actual != expected:
            raise _Unsupported(
                "Pyoxigraph storage cannot retain the supplied exact RDF terms"
            )
        native_bindings: dict[
            ox.Variable, ox.NamedNode | ox.BlankNode | ox.Literal | ox.Triple
        ] = {ox.Variable(name): _native(value) for name, value in bindings.items()}
        native_result = store.query(
            text,
            prefixes={name: str(iri) for name, iri in namespaces.items()},
            use_default_graph_as_union=config["default_graph"] == "union",
            substitutions=native_bindings,
        )
        if isinstance(native_result, ox.QueryBoolean):
            return {
                "status": "result",
                "kind": "ASK",
                "answer": bool(native_result),
                "complete": True,
            }
        if not isinstance(native_result, ox.QuerySolutions):
            raise _Unsupported("Workload profile supports ASK and SELECT only")
        variables = tuple(value.value for value in native_result.variables)
        rows = _rows(native_result, variables, budget, maximum, output_cap, native=True)
    return {
        "status": "result",
        "kind": "SELECT",
        "variables": variables,
        "rows": rows,
        "complete": True,
        "message": "",
    }


def process_request(payload: bytes, cap: int) -> bytes:
    """Return exact native identity, control counters and one complete typed result."""
    budget = None
    reply: dict[str, object] = {
        "protocol": 1,
        "status": "failed",
        "version": None,
        "python": platform.python_version(),
        "result": None,
        "message": "",
        "controls": None,
    }
    try:
        request = object_value(decode_json(payload, max_bytes=cap), location="workload")
        limits = Limits(
            **cast(dict[str, int], object_value(request["limits"], location="limits"))
        )
        budget = ControlBudget(limits, request["allowances"])
        _measure = json_bound(request, budget)
        budget.consume("bytes", _measure * 2)
        config = object_value(request["config"], location="config")
        if config.get("engine") not in ("rdflib", "pyoxigraph"):
            raise InputError("Invalid workload engine")
        reply["version"] = version(cast(str, config["engine"]))
        reply["result"] = evaluate(request, budget)
        reply["status"] = "completed"
        size = json_bound(reply, budget) + 1024
        if size > cast(int, request["output_cap"]):
            raise LimitExceeded("Workload reply exceeds its response cap")
        budget.consume("bytes", size * 2)
        budget.consume("work", size)
    except Exception as error:
        reply["result"] = None
        reply["status"] = (
            "exhausted"
            if isinstance(error, LimitExceeded | MemoryError | RecursionError)
            else "unsupported"
            if isinstance(error, _Unsupported | ResourceError)
            else "failed"
        )
        reply["message"] = str(error)[:1024] or type(error).__name__
    reply["controls"] = budget.consumed() if budget is not None else None
    raw = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode()
    if len(raw) > cap:
        return b"{}"
    return raw


def main() -> int:
    """Read one capped request inside the coordinator-owned worker lifetime."""
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
