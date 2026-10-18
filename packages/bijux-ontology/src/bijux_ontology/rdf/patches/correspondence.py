"""Owned maintained isomorphism with uniquely qualified blank-reference bindings."""

import json
import math
import sys
import threading
import time
from dataclasses import asdict

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.term import Node

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, BudgetKind, Limits
from bijux_ontology.rdf.execution import checkpoint, execute_worker
from bijux_ontology.reports import Issue

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


class Operation(Budget):
    """Share host counters while qualifying every stage with one absolute deadline."""

    def __init__(
        self,
        limits: Limits | None,
        budget: Budget | None,
        timeout: float,
        cancel: threading.Event | None,
    ) -> None:
        """Validate execution ownership before reading any caller inventory."""
        if (
            limits is not None
            and not isinstance(limits, Limits)
            or budget is not None
            and not isinstance(budget, Budget)
        ):
            raise InputError("RDF patch operations require Limits and Budget objects")
        if limits is not None and budget is not None and limits != budget.limits:
            raise InputError("RDF patch limits conflict with the shared budget")
        if (
            type(timeout) not in (float, int)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise InputError("RDF patch timeout must be finite and positive")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise InputError("RDF patch cancellation requires threading.Event")
        shared = budget if budget is not None else Budget(limits)
        super().__init__(shared.limits)
        self.shared, self.cancel = shared, cancel
        self.deadline = time.monotonic() + timeout

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Charge the original operation counters without restarting its allowance."""
        checkpoint(self.deadline, self.cancel)
        self.shared.consume(kind, amount)

    def remaining(self, kind: BudgetKind) -> int:
        """Read the actual shared allowance, including any enclosing operation."""
        return self.shared.remaining(kind)

    def _retain_issue(self, issue: Issue, *, repeated: bool) -> None:
        """Preserve completed paid diagnostics across collector ownership transfer."""
        self.shared._retain_issue(issue, repeated=repeated)


class _ControlBudget(Budget):
    """Bound worker-owned preparation to the caller's remaining allowances.

    Maintained isomorphism internals expose no work counter and remain bounded
    by the enclosing owned process deadline. These counters cover our explicit
    decoding, projection and correspondence-control operations only.
    """

    def __init__(self, limits: Limits, allowances: object) -> None:
        """Accept only complete finite nonnegative per-kind allowances."""
        super().__init__(limits)
        if not isinstance(allowances, dict) or set(allowances) != set(_KINDS):
            raise InputError("Malformed RDF patch worker allowances")
        self.allowances: dict[BudgetKind, int] = {}
        for kind in _KINDS:
            amount = allowances[kind]
            if type(amount) is not int or not 0 <= amount <= super().remaining(kind):
                raise InputError("Invalid RDF patch worker allowance")
            self.allowances[kind] = amount

    def remaining(self, kind: BudgetKind) -> int:
        """Subtract completed worker work from this request's initial allowance."""
        capacity = (
            self.limits.max_total_bytes
            if kind == "bytes"
            else getattr(self.limits, f"max_{kind}")
        )
        return self.allowances[kind] - (capacity - super().remaining(kind))

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Refuse work before it exceeds the caller's available cumulative bound."""
        if type(amount) is not int or amount < 0:
            raise InputError("Budget consumption must be a nonnegative integer")
        if amount > self.remaining(kind):
            raise LimitExceeded("RDF patch worker exhausted its shared allowance")
        super().consume(kind, amount)

    def consumed(self) -> dict[BudgetKind, int]:
        """Return exact completed owned work for validated host adoption."""
        return {kind: self.allowances[kind] - self.remaining(kind) for kind in _KINDS}


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


def _json_bound(value: object, budget: Budget) -> int:
    """Reserve a conservative JSON encoding before allocating its byte payload."""
    budget.consume("work")
    if isinstance(value, str):
        budget.consume("work", len(value))
        return 2 + 12 * len(value)
    if value is None or type(value) is bool:
        return 5
    if type(value) is int:
        if value.bit_length() > min(14000, 3 * budget.limits.max_bytes):
            raise LimitExceeded("RDF patch integer metadata exceeds its byte bound")
        return 4 + value.bit_length()
    if isinstance(value, list | tuple):
        return 2 + sum(1 + _json_bound(item, budget) for item in value)
    if isinstance(value, dict):
        return 2 + sum(
            2 + _json_bound(key, budget) + _json_bound(item, budget)
            for key, item in value.items()
        )
    raise InputError("Unexpected RDF patch protocol value")


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
    size = _json_bound(request, operation)
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
        budget = _ControlBudget(Limits(**request["limits"]), request["allowances"])
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
