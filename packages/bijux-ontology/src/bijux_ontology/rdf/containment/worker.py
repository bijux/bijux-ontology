"""A fixed maintained SPARQL basic graph pattern over supplied owned RDF."""

import json
import sys
from collections.abc import Iterator, Mapping
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef, Variable
from rdflib.plugins import sparql
from rdflib.plugins.sparql.processor import SPARQLProcessor
from rdflib.term import Identifier, Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Limits
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.queries.syntax import prepare_local_query
from bijux_ontology.rdf.records.codecs import read_witness, term_data
from bijux_ontology.rdf.records.materialization import materialize
from bijux_ontology.rdf.records.types import PatchBlank, PatchLiteral, PatchValue
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.terms import literal_datatype, literal_term


def evaluate(request: dict[str, object], budget: ControlBudget) -> dict[str, object]:
    """Let the maintained engine find one coherent substitution, without inference."""
    if set(request) != {"data", "template"}:
        raise InputError("Malformed containment request")
    data = read_witness(request["data"], budget)
    template = read_witness(request["template"], budget)
    if data.kind != "graph" or template.kind != "graph":
        raise InputError("Simple entailment requires physical graph projections")
    original = prepare_rdf(Graph(), budget=budget)
    actual_blanks: dict[str, BNode] = {}
    source = materialize(data, original, budget, bindings=actual_blanks)
    graph = source._read(budget=budget)
    budget.consume("states", len(actual_blanks))
    budget.consume("bytes", 64 * len(actual_blanks))
    reverse = {node: PatchBlank(token) for token, node in actual_blanks.items()}
    constants: dict[PatchValue, str] = {}
    variables: dict[str, str] = {}
    bindings: dict[str, Identifier] = {}

    def expression(term: PatchValue) -> str:
        """Bind constants as exact RDF terms, avoiding query literal reconstruction."""
        budget.consume("work")
        if isinstance(term, PatchBlank):
            if term.token not in variables:
                budget.consume("states")
                budget.consume("bytes", 128)
                variables[term.token] = "b" + str(len(variables))
            return "?" + variables[term.token]
        if term not in constants:
            budget.consume("states", 2)
            budget.consume("bytes", 256)
            name = "c" + str(len(constants))
            constants[term] = name
            if isinstance(term, URIRef):
                value: Identifier = term
            else:
                size = len(term.lexical) + len(term.datatype) + len(term.language or "")
                budget.consume("work", size)
                budget.consume("bytes", 32 * size + 128)
                value = literal_term(
                    term.lexical,
                    datatype=None if term.language else term.datatype,
                    language=term.language,
                )
            bindings[name] = value
        return "?" + constants[term]

    patterns = []
    for quad in template.quads:
        budget.consume("work")
        budget.consume("states")
        budget.consume("bytes", 256)
        patterns.append(
            " ".join(
                expression(term) for term in (quad.subject, quad.predicate, quad.value)
            )
            + " ."
        )
    budget.consume("bytes", 256 * (len(patterns) + len(variables) + 1))
    clause = " WHERE { " + " ".join(patterns) + " }"
    text = (
        "SELECT "
        + " ".join("?" + name for name in variables.values())
        + clause
        + " LIMIT 1"
        if variables
        else "ASK" + clause
    )
    sparql.SPARQL_LOAD_GRAPHS = False
    sparql.SPARQL_DEFAULT_GRAPH_UNION = False
    query = prepare_local_query(text, {}, budget)
    result = SPARQLProcessor(graph).query(query, initBindings=bindings)  # type: ignore[no-untyped-call]
    if not variables:
        answer = result.get("askAnswer")
        if type(answer) is not bool:
            raise ResourceError("Native containment ASK returned no boolean")
        return {"entailed": answer, "bindings": []}
    if result.get("type_") != "SELECT":
        raise ResourceError("Native containment SELECT returned no solution table")
    rows = cast(Iterator[Mapping[Variable, Node]], iter(result["bindings"]))
    row = next(rows, None)
    if row is None:
        return {"entailed": False, "bindings": []}
    witnesses = []
    for token, name in variables.items():
        budget.consume("work")
        value = row.get(Variable(name))
        if value is None:
            raise ResourceError("Native containment witness left a variable unbound")
        if isinstance(value, BNode):
            if value not in reverse:
                raise ResourceError("Native witness has no selected data ownership")
            reference: PatchValue = reverse[value]
        elif isinstance(value, URIRef):
            reference = value
        elif isinstance(value, Literal):
            size = (
                len(str(value))
                + len(str(literal_datatype(value)))
                + len(value.language or "")
            )
            budget.consume("bytes", 32 * size + 128)
            budget.consume("work", size)
            reference = PatchLiteral(
                str(value), str(literal_datatype(value)), value.language
            )
        else:
            raise ResourceError("Native containment returned an unsupported RDF term")
        budget.consume("states", 2)
        budget.consume("bytes", 128)
        witnesses.append([token, term_data(reference, budget)])
    return {"entailed": True, "bindings": witnesses}


def main() -> None:
    """Read one finite operation and return exact owned-control counters."""
    cap = int(sys.argv[1])
    payload = sys.stdin.buffer.read(cap + 1)
    if len(payload) > cap:
        raise LimitExceeded("Containment input exceeds protocol bound")
    envelope = object_value(decode_json(payload, max_bytes=cap), location="request")
    limits = Limits(**cast(dict[str, int], envelope["limits"]))
    budget = ControlBudget(limits, envelope["allowances"])
    reply: dict[str, object] = {"status": "ok", "data": None, "message": ""}
    try:
        reply["data"] = evaluate(
            object_value(envelope["request"], location="data"), budget
        )
        size = json_bound(reply, budget) + 512
        budget.consume("bytes", size * 2)
        budget.consume("work", size)
    except (InputError, ValueError, RecursionError) as error:
        reply.update(
            status="exhausted"
            if isinstance(error, LimitExceeded | RecursionError)
            else "unverified",
            data=None,
            message=str(error)[:1024],
        )
    reply["controls"] = budget.consumed()
    content = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    if len(content) > cap:
        raise LimitExceeded("Containment reply exceeds protocol bound")
    sys.stdout.buffer.write(content)


if __name__ == "__main__":
    main()
