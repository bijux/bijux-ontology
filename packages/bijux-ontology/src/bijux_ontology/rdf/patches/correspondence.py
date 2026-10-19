"""Owned maintained isomorphism with uniquely qualified blank-reference bindings."""

import json
import sys
import time
from dataclasses import asdict

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.term import Node

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import Budget, BudgetKind, Limits

from .codecs import read_witness, term_data, witness_data
from .types import PatchBlank, PatchUnavailable, PatchValue, SnapshotWitness

_BASE = "urn:bijux:ontology:patch:"
_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def blank_tokens(witness: SnapshotWitness, budget: Budget) -> set[str]:
    """Read qualified tokens without constructing an uncharged flattened vector."""
    tokens = set()

    def retain(term: PatchValue | None) -> None:
        """Charge each visited reference before retaining a distinct token."""
        budget.consume("work")
        if isinstance(term, PatchBlank) and term.token not in tokens:
            budget.consume("states")
            tokens.add(term.token)

    retain(witness.graph_selection)
    for graph in witness.graphs:
        retain(graph)
    for quad in witness.quads:
        retain(quad.subject)
        retain(quad.value)
        retain(quad.graph)
    return tokens


def compare_witnesses(
    left: SnapshotWitness,
    right: SnapshotWitness,
    operation: Operation,
    *,
    required: tuple[str, ...] = (),
    fixed: tuple[tuple[str, str], ...] = (),
) -> tuple[bool, dict[str, str]]:
    """Prove equality and requested unique bindings through the maintained worker."""
    operation.consume("work")
    request = {
        "left": witness_data(left, operation),
        "right": witness_data(right, operation),
        "required": list(required),
        "fixed": [list(pair) for pair in fixed],
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in _KINDS},
    }
    size = json_bound(request, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("RDF patch protocol exceeds max_bytes")
    operation.consume("bytes", size)
    allowances = {kind: operation.remaining(kind) for kind in _KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("RDF patch exceeded its wall-clock deadline")
    raw = execute_worker(
        "bijux_ontology.rdf.patches.correspondence",
        payload,
        cap=operation.limits.max_bytes,
        timeout=remaining,
        cancel=operation.cancel,
    )
    operation.consume("bytes", len(raw))
    response = decode_json(raw, max_bytes=operation.limits.max_bytes)
    if not isinstance(response, dict) or response.get("status") not in (
        "result",
        "exhausted",
        "input_error",
    ):
        raise ResourceError("Invalid RDF patch worker response")
    consumed = response.get("consumed")
    if (
        not isinstance(consumed, dict)
        or set(consumed) != set(_KINDS)
        or any(
            type(consumed[kind]) is not int
            or not 0 <= consumed[kind] <= allowances[kind]
            for kind in _KINDS
        )
    ):
        raise ResourceError("Malformed RDF patch worker accounting completion")
    for kind in _KINDS:
        operation.consume(kind, consumed[kind])
    if response["status"] == "exhausted":
        raise LimitExceeded("RDF patch comparison exhausted its allowance")
    if response["status"] == "input_error":
        raise ResourceError("RDF patch worker rejected its structural request")
    if (
        set(response) != {"status", "equal", "pairs", "ambiguous", "consumed"}
        or type(response["equal"]) is not bool
        or not isinstance(response["pairs"], list)
        or not isinstance(response["ambiguous"], list)
    ):
        raise ResourceError("Malformed RDF patch correspondence completion")
    left_tokens, right_tokens = (
        blank_tokens(left, operation),
        blank_tokens(right, operation),
    )
    pairs: dict[str, str] = {}
    used: set[str] = set()
    for pair in response["pairs"]:
        operation.consume("work")
        operation.consume("states")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
            or pair[0] not in left_tokens
            or pair[1] not in right_tokens
            or pair[0] in pairs
            or pair[1] in used
        ):
            raise ResourceError("Invalid RDF patch blank correspondence")
        pairs[pair[0]] = pair[1]
        used.add(pair[1])
    ambiguous = response["ambiguous"]
    if (
        any(not isinstance(token, str) or token not in required for token in ambiguous)
        or response["equal"]
        and set(required) - pairs.keys() != set(ambiguous)
        or not response["equal"]
        and (pairs or ambiguous)
    ):
        raise ResourceError("Incomplete RDF patch correspondence evidence")
    if response["equal"] and any(pairs.get(a) != b for a, b in fixed):
        raise ResourceError("RDF patch worker did not retain fixed bindings")
    if ambiguous:
        raise PatchUnavailable(
            "Affected base blank references have ambiguous structural correspondence"
        )
    return response["equal"], pairs


def _graph(witness: SnapshotWitness, budget: Budget) -> Graph:
    """Encode exact quad/inventory/profile records without caller-term collisions."""
    graph = Graph(bind_namespaces="none")
    budget.consume("states", 1 + len(witness.graphs) + len(witness.quads))
    rows: list[tuple[PatchValue | None, ...]] = [
        (URIRef(_BASE + witness.kind), witness.graph_selection)
    ]
    rows.extend((name,) for name in witness.graphs)
    rows.extend((q.subject, q.predicate, q.value, q.graph) for q in witness.quads)
    for index, row in enumerate(rows):
        budget.consume("work")
        budget.consume("states", len(row) + 1)
        budget.consume("triples", len(row) + 1)
        record = BNode("record:" + str(index))
        graph.add(
            (
                record,
                URIRef(_BASE + "kind"),
                Literal(
                    "scope" if index == 0 else "graph" if len(row) == 1 else "quad"
                ),
            )
        )
        for position, term in enumerate(row):
            value: Node
            if isinstance(term, PatchBlank):
                value = BNode("original:" + term.token)
            else:
                encoded = json.dumps(
                    term_data(term, budget), ensure_ascii=True, separators=(",", ":")
                )
                budget.consume("bytes", len(encoded))
                value = Literal(encoded, normalize=False)
            graph.add((record, URIRef(_BASE + "position:" + str(position)), value))
    return graph


def _marked(graph: Graph, pairs: tuple[str, ...], budget: Budget) -> Graph:
    """Copy a bounded comparison graph and distinguish only requested originals."""
    budget.consume("triples", len(graph) + len(pairs))
    budget.consume("states", len(graph) + len(pairs))
    marked = Graph(bind_namespaces="none")
    for triple in graph:
        budget.consume("work")
        marked.add(triple)
    for index, token in enumerate(pairs):
        budget.consume("work")
        marked.add(
            (
                BNode("original:" + token),
                URIRef(_BASE + "binding:" + str(index)),
                Literal("selected"),
            )
        )
    return marked


def _execute(request: dict[str, object], budget: Budget) -> dict[str, object]:
    """Return only maintained equality and uniquely established original mappings."""
    left = read_witness(request["left"], budget)
    right = read_witness(request["right"], budget)
    required, fixed = request["required"], request["fixed"]
    if (
        not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or len(set(required)) != len(required)
        or not isinstance(fixed, list)
    ):
        raise InputError("Malformed RDF patch correspondence constraints")
    left_tokens, right_tokens = blank_tokens(left, budget), blank_tokens(right, budget)
    pairs: dict[str, str] = {}
    for pair in fixed:
        budget.consume("work")
        budget.consume("states")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
            or pair[0] not in left_tokens
            or pair[1] not in right_tokens
            or pair[0] in pairs
            or pair[1] in pairs.values()
        ):
            raise InputError("Invalid fixed blank correspondence")
        pairs[pair[0]] = pair[1]
    if any(token not in left_tokens for token in required):
        raise InputError("Required blank reference is outside the base witness")
    if not left_tokens and not right_tokens:
        # Qualified ground terms need no structural blank-node correspondence.
        count = (
            len(left.quads) + len(right.quads) + len(left.graphs) + len(right.graphs)
        )
        budget.consume("work", count + 2)
        budget.consume("states", count)
        budget.consume("bytes", 128 + 64 * count)
        equal = (
            left.kind == right.kind
            and left.graph_selection == right.graph_selection
            and set(left.graphs) == set(right.graphs)
            and set(left.quads) == set(right.quads)
        )
        return {"status": "result", "equal": equal, "pairs": [], "ambiguous": []}
    left_graph, right_graph = _graph(left, budget), _graph(right, budget)
    first = tuple(pairs)
    second = tuple(pairs.values())
    equal = isomorphic(
        _marked(left_graph, first, budget), _marked(right_graph, second, budget)
    )
    if not equal:
        return {"status": "result", "equal": False, "pairs": [], "ambiguous": []}
    ambiguous = []
    for token in required:
        budget.consume("work")
        if token in pairs:
            continue
        possible = []
        budget.consume("work", len(right_tokens) + len(pairs))
        budget.consume("states", len(right_tokens))
        for candidate in right_tokens - set(pairs.values()):
            budget.consume("work")
            first = (*pairs, token)
            second = (*pairs.values(), candidate)
            if isomorphic(
                _marked(left_graph, first, budget), _marked(right_graph, second, budget)
            ):
                budget.consume("states")
                possible.append(candidate)
                if len(possible) > 1:
                    break
        if len(possible) == 1:
            pairs[token] = possible[0]
        else:
            ambiguous.append(token)
    return {
        "status": "result",
        "equal": True,
        "pairs": list(pairs.items()),
        "ambiguous": ambiguous,
    }


def main() -> int:
    """Execute one finite correspondence request inside the owned process group."""
    try:
        cap = int(sys.argv[1])
        if cap <= 0:
            return 2
        request = decode_json(sys.stdin.buffer.read(cap + 1), max_bytes=cap)
        if (
            not isinstance(request, dict)
            or set(request)
            != {"left", "right", "required", "fixed", "limits", "allowances"}
            or not isinstance(request["limits"], dict)
        ):
            return 2
        budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
        try:
            result = _execute(request, budget)
        except (LimitExceeded, MemoryError):
            result = {"status": "exhausted"}
        except (InputError, TypeError, ValueError):
            result = {"status": "input_error"}
        result["consumed"] = budget.consumed()
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        if len(output) > cap:
            return 2
        sys.stdout.buffer.write(output)
        return 0
    except (InputError, ValueError, IndexError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
