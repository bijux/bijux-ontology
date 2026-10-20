"""One isolated maintained CONSTRUCT/local UPDATE over an exact owned graph."""

import json
import sys
from contextlib import redirect_stdout
from typing import cast

import rdflib
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.plugins import sparql
from rdflib.plugins.sparql.processor import SPARQLProcessor, SPARQLUpdateProcessor
from rdflib.term import Identifier, Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, Limits
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.records.codecs import read_term, read_witness, witness_data
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchValue,
    SnapshotWitness,
)
from bijux_ontology.rdf.terms import (
    bound_node,
    literal_datatype,
    literal_term,
    validate_triple,
)

from .preparation import (
    TransformUnavailable,
    checked_program,
    graph_from_witness,
    variable_name,
)


def _result(
    graph: Graph, scope: SnapshotWitness, blanks: dict[str, BNode], budget: Budget
) -> SnapshotWitness:
    """Preserve actual input blank lineage and keep generated identities separate."""
    count = len(graph)
    if count > budget.limits.max_triples:
        raise LimitExceeded("Transformation output exceeds max_triples")
    budget.consume("states", len(blanks) + count)
    reverse = {value: PatchBlank(token) for token, value in blanks.items()}
    fresh: dict[BNode, PatchBlank] = {}

    def portable(value: Node) -> PatchValue:
        """Use maintained term equality only inside this one actual native scope."""
        bound_node(value, budget)
        if isinstance(value, BNode):
            if value in reverse:
                return reverse[value]
            if value not in fresh:
                budget.consume("states")
                budget.consume("bytes", 64)
                fresh[value] = PatchBlank("generated:" + str(len(fresh)))
            return fresh[value]
        if isinstance(value, Literal):
            return PatchLiteral(
                str(value), str(literal_datatype(value)), value.language
            )
        if isinstance(value, URIRef):
            return value
        raise InputError("Transformation output has an unsupported RDF term")

    quads = []
    for subject, predicate, value in graph:
        budget.consume("work")
        budget.consume("triples")
        owned_subject = portable(subject)
        owned_predicate = portable(predicate)
        owned_value = portable(value)
        validate_triple(subject, predicate, value)
        quads.append(
            PatchQuad(
                cast(URIRef | PatchBlank, owned_subject),
                cast(URIRef, owned_predicate),
                owned_value,
                scope.graph_selection,
            )
        )
    return SnapshotWitness(
        scope.kind, scope.graph_selection, tuple(quads), scope.graphs
    )


def _execute(request: dict[str, object], budget: Budget) -> dict[str, object]:
    """Invoke public native processors with separately bound RDF parameters."""
    initial = read_witness(request["source"], budget)
    graph, blanks = graph_from_witness(initial, budget)
    text, kind, records = request["text"], request["kind"], request["bindings"]
    if (
        type(text) is not str
        or kind not in ("CONSTRUCT", "UPDATE")
        or not isinstance(records, list)
    ):
        raise InputError("Malformed SPARQL transformation request")
    budget.consume("states", len(records))
    bindings: dict[str, Identifier] = {}
    for raw in records:
        budget.consume("work")
        if not isinstance(raw, list) or len(raw) != 2:
            raise InputError("Malformed SPARQL parameter record")
        name = variable_name(raw[0], budget)
        if name in bindings:
            raise InputError("SPARQL parameter names must be distinct")
        value = read_term(raw[1], budget)
        if isinstance(value, PatchBlank):
            if value.token not in blanks:
                raise InputError("Unowned SPARQL blank parameter")
            bindings[name] = blanks[value.token]
        elif isinstance(value, PatchLiteral):
            bindings[name] = literal_term(
                value.lexical, datatype=value.datatype, language=value.language
            )
        elif isinstance(value, URIRef):
            bindings[name] = value
        else:
            raise InputError("Unbound SPARQL parameter value")
    compiled = checked_program(text, kind, budget)
    if kind == "CONSTRUCT":
        result = SPARQLProcessor(graph).query(compiled, initBindings=bindings)  # type: ignore[no-untyped-call]
        if result["type_"] != "CONSTRUCT" or not isinstance(result["graph"], Graph):
            raise InputError("Native transformation returned no CONSTRUCT graph")
        graph = result["graph"]
    else:
        SPARQLUpdateProcessor(graph).update(compiled, initBindings=bindings)  # type: ignore[no-untyped-call]
    witness = _result(graph, initial, blanks, budget)
    return {"status": "result", "target": witness_data(witness, budget)}


def process_request(payload: bytes, cap: int) -> bytes:
    """Bound typed worker responses without exposing unqualified exception text."""
    budget = None
    response: dict[str, object]
    previous = (
        rdflib.NORMALIZE_LITERALS,
        sparql.SPARQL_DEFAULT_GRAPH_UNION,
        sparql.SPARQL_LOAD_GRAPHS,
    )
    try:
        request = decode_json(payload, max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Malformed SPARQL transformation protocol")
        budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
        rdflib.NORMALIZE_LITERALS = False
        sparql.SPARQL_DEFAULT_GRAPH_UNION = False
        sparql.SPARQL_LOAD_GRAPHS = False
        with redirect_stdout(sys.stderr):
            response = _execute(request, budget)
    except (LimitExceeded, MemoryError, RecursionError):
        response = {"status": "exhausted"}
    except (TransformUnavailable, LiteralScopeUnsupported):
        response = {"status": "unverified"}
    except Exception:  # noqa: BLE001 - isolated engine boundary returns typed failure
        response = {"status": "input_error"}
    finally:
        (
            rdflib.NORMALIZE_LITERALS,
            sparql.SPARQL_DEFAULT_GRAPH_UNION,
            sparql.SPARQL_LOAD_GRAPHS,
        ) = previous
    if budget is not None:
        try:
            size = json_bound(response, budget)
            if size > cap:
                raise LimitExceeded("Transformation result exceeds protocol bound")
            budget.consume("bytes", size)
        except LimitExceeded:
            response = {"status": "exhausted"}
        response["consumed"] = budget.consumed()
    else:
        response["consumed"] = dict.fromkeys(BUDGET_KINDS, 0)
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    if len(encoded) > cap:
        response = {
            "status": "exhausted",
            "consumed": budget.consumed()
            if budget is not None
            else dict.fromkeys(BUDGET_KINDS, 0),
        }
        encoded = json.dumps(response, separators=(",", ":")).encode("ascii")
    return encoded


def main() -> int:
    """Read and write one bounded request through the existing owned transport."""
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
