"""Isolated native snapshot ingestion and bounded read-only database operations."""

import json
import sys
from collections.abc import Mapping
from typing import Any

import pyoxigraph as ox
import rdflib
from rdflib import Literal
from rdflib.namespace import XSD
from rdflib.plugins.sparql.parserutils import CompValue

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.rdf.queries.syntax import prepare_local_query

from .native import native_term, qualify_literal, rdf_term


def _inventory(store: ox.Store, budget: Budget) -> dict[str, object]:
    """Inspect native rows without retaining an in-memory dataset or term index."""
    count = 0
    for quad in store:
        budget.consume("work")
        budget.consume("triples")
        for value in (quad.subject, quad.predicate, quad.object, quad.graph_name):
            term_data(rdf_term(value))
        count += 1
    names = []
    for name in store.named_graphs():
        budget.consume("work")
        budget.consume("resources")
        budget.consume("states")
        names.append(term_data(rdf_term(name)))
    names.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return {"quads": count, "graphs": names, "backend": ox.__version__}


def _query(
    request: dict[str, Any], store: ox.Store, budget: Budget
) -> dict[str, object]:
    """Parse the same finite local query profile, then execute the maintained store."""
    query = prepare_local_query(request["query"], {}, budget)
    pending = [query.algebra]
    probe = ox.Store()
    while pending:
        budget.consume("work")
        budget.consume("states")
        value = pending.pop()
        if isinstance(value, Literal):
            encoded = native_term(value)
            assert isinstance(encoded, ox.Literal)
            qualify_literal(encoded, probe)
        elif isinstance(value, CompValue):
            if value.name == "Builtin_STRDT" or (
                value.name == "Function" and str(value.get("iri")).startswith(str(XSD))
            ):
                raise LiteralScopeUnsupported(
                    "Datatype constructors are outside the disk query lexical profile"
                )
            pending.extend(value.values())
        elif isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
    scope = request["scope"]
    options: dict[str, Any] = {}
    if scope == "default":
        options = {"default_graph": ox.DefaultGraph(), "named_graphs": []}
    elif scope == "named":
        name = request["graph"]
        graph = (
            ox.BlankNode(name["value"])
            if name["kind"] == "blank"
            else ox.NamedNode(name["value"])
        )
        options = {"default_graph": graph, "named_graphs": []}
    elif scope == "union":
        options = {"use_default_graph_as_union": True, "named_graphs": []}
    elif scope != "dataset":
        raise InputError("Unknown native query graph scope")
    result = store.query(request["query"], **options)
    if isinstance(result, ox.QueryBoolean):
        return {
            "status": "result",
            "kind": "ASK",
            "answer": bool(result),
            "complete": True,
        }
    if not isinstance(result, ox.QuerySolutions):
        raise InputError("Only ASK and SELECT are supported")
    variables = [value.value for value in result.variables]
    budget.consume("states", len(variables))
    rows: list[list[dict[str, object] | None]] = []
    complete, message = True, ""
    try:
        for solution in result:
            budget.consume("work")
            if len(rows) >= request["max_rows"]:
                raise LimitExceeded("SELECT result exceeds max_rows")
            budget.consume("states", len(variables))
            row = [
                term_data(rdf_term(value)) if value is not None else None
                for value in solution
            ]
            budget.consume("bytes", json_bound(row, budget))
            rows.append(row)
    except LimitExceeded as error:
        complete, message = False, str(error)
    return {
        "status": "result",
        "kind": "SELECT",
        "variables": variables,
        "rows": rows,
        "complete": complete,
        "message": message,
    }


def evaluate(request: dict[str, Any], budget: Budget) -> dict[str, object]:
    """Use only an owned closed store, or create a private mutable build owner."""
    rdflib.NORMALIZE_LITERALS = False
    if request["operation"] == "build":
        store = ox.Store(request["mutable"])
        probe = ox.Store()
        format_ = {"nt": ox.RdfFormat.N_TRIPLES, "nquads": ox.RdfFormat.N_QUADS}.get(
            request["format"]
        )
        if format_ is None:
            raise InputError("Disk ingestion supports N-Triples and N-Quads")

        def parsed_rows() -> Any:
            """Qualify a maintained streaming parse before native bulk storage."""
            for quad in ox.parse(
                path=request["source"], format=format_, rename_blank_nodes=False
            ):
                budget.consume("work")
                budget.consume("triples")
                for value in (
                    quad.subject,
                    quad.predicate,
                    quad.object,
                    quad.graph_name,
                ):
                    term = term_data(rdf_term(value))
                    budget.consume("bytes", json_bound(term, budget))
                    if isinstance(value, ox.Literal):
                        qualify_literal(value, probe)
                yield quad

        store.bulk_extend(parsed_rows())
        inventory = _inventory(store, budget)
        store.flush()
        store.backup(request["database"])
        return {"status": "inventory", **inventory}
    store = ox.Store.read_only(request["database"])
    if request["operation"] == "inventory":
        return {"status": "inventory", **_inventory(store, budget)}
    if request["operation"] == "query":
        return _query(request, store, budget)
    if request["operation"] == "materialize":
        scope, selected = request["scope"], request.get("graph")
        name: ox.DefaultGraph | ox.NamedNode | ox.BlankNode | None = None
        if scope == "default":
            name = ox.DefaultGraph()
        elif scope == "named":
            if not isinstance(selected, dict):
                raise InputError("Named materialization needs a graph resource")
            name = (
                ox.BlankNode(selected["value"])
                if selected["kind"] == "blank"
                else ox.NamedNode(selected["value"])
            )
        elif scope not in ("dataset", "union"):
            raise InputError("Unknown materialization scope")
        for count, _ in enumerate(store.quads_for_pattern(None, None, None, name), 1):
            budget.consume("work")
            if count > request["maximum"]:
                raise LimitExceeded(
                    "Disk projection exceeds explicit materialization limit"
                )
        rows = []
        for quad in store.quads_for_pattern(None, None, None, name):
            budget.consume("work")
            budget.consume("triples")
            budget.consume("states")
            row = [
                term_data(rdf_term(value))
                for value in (
                    quad.subject,
                    quad.predicate,
                    quad.object,
                    quad.graph_name,
                )
            ]
            budget.consume("bytes", json_bound(row, budget))
            rows.append(row)
        return {"status": "materialized", "rows": rows}
    raise InputError("Unsupported native database operation")


def main() -> int:
    """Return typed bounded completion evidence through the owned transport."""
    cap = int(sys.argv[1])
    budget = None
    try:
        request = decode_json(sys.stdin.buffer.read(cap + 1), max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Expected disk operation object")
        budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
        response = evaluate(request, budget)
    except Exception as error:  # noqa: BLE001 - typed isolated native boundary
        response = {
            "status": "exhausted"
            if isinstance(error, (LimitExceeded, MemoryError))
            else "unverified"
            if isinstance(error, LiteralScopeUnsupported)
            else "input_error",
            "message": str(error)[:2000],
        }
    if budget is not None:
        response["consumed"] = budget.consumed()
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > cap:
        encoded = (
            b'{"status":"exhausted",'
            b'"message":"Disk response exceeds protocol byte bound"}'
        )
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
