"""Isolated native snapshot ingestion and bounded read-only database operations."""

import json
import sys
from typing import Any

import pyoxigraph as ox
import rdflib

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import term_data

from .native import qualify_literal, rdf_term
from .queries import query_native


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
        return query_native(request, store, budget)
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
