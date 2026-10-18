"""Typed read-only local query results with explicit owned RDF scope."""

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Literal as Kind
from typing import cast

from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, BudgetKind, Limits
from bijux_ontology.rdf.execution import checkpoint, execute_worker
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.protocol import QueryTerm, read_term, term_data
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import validate_triple
from bijux_ontology.reports import Issue, IssueKind, Report


class _CaptureBudget(Budget):
    """Check the host deadline during metadata scans owned by RDF snapshots."""

    def __init__(self, limits: Limits, deadline: float, cancel: threading.Event | None):
        super().__init__(limits)
        self.deadline, self.cancel = deadline, cancel

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        checkpoint(self.deadline, self.cancel)
        super().consume(kind, amount)


@dataclass(frozen=True)
class QueryScope:
    """A query dataset or a graph projection used as its sole default graph.

    Dataset preserves authored default/named graphs. Default, named and union
    project only selected triples into a graph; GRAPH cannot escape that choice.
    Named blanks must be exact identifiers from a PreparedSource dataset export.
    """

    kind: Kind["default", "named", "dataset", "union"]
    graph_name: GraphSelection | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "default",
            "named",
            "dataset",
            "union",
        }:
            raise InputError("Query scope must be default, named, dataset or union")
        if self.kind == "named":
            if self.graph_name is None:
                raise InputError("Named query scope requires an authored graph name")
            if not isinstance(self.graph_name, BNode):
                name = absolute_iri(str(self.graph_name))
                if name == str(DATASET_DEFAULT_GRAPH_ID):
                    raise InputError("Use default scope for the RDF default graph")
                object.__setattr__(self, "graph_name", URIRef(name))
        elif self.graph_name is not None:
            raise InputError("Only named query scope accepts a graph name")


@dataclass(frozen=True)
class QueryResult:
    """Completed query truth beside raw answers and exact immutable RDF terms.

    A false ASK is a completed answer, not a diagnostic disagreement. SELECT
    row positions align with variables; None is unbound. Blank terms are local
    to this result and never imply identity with another query or input export.
    """

    report: Report
    scope: QueryScope
    kind: Kind["ASK", "SELECT"] | None = None
    answer: bool | None = None
    variables: tuple[str, ...] = ()
    rows: tuple[tuple[QueryTerm | None, ...], ...] = ()


def _graphs(
    source: Graph | PreparedSource,
    scope: QueryScope,
    budget: Budget,
) -> Iterator[tuple[QueryTerm | None, Graph]]:
    """Inspect only the requested resource scope through the owned read boundary."""
    if isinstance(source, PreparedSource):
        if scope.kind == "dataset":
            if source.graph_name is not None or source.union:
                raise InputError("Dataset query conflicts with the resource selection")
            yield None, source._read("@default", budget=budget)
            for name in source._graph_names(budget=budget):
                yield name, source._read(name, budget=budget)
        else:
            if isinstance(scope.graph_name, BNode) and not any(
                name is scope.graph_name for name in source._graph_names(budget=budget)
            ):
                raise InputError("Blank graph name must come from its owned snapshot")
            graph_name = "@default" if scope.kind == "default" else scope.graph_name
            if isinstance(graph_name, URIRef):
                graph_name = str(graph_name)
            yield (
                None,
                source._read(graph_name, union=scope.kind == "union", budget=budget),
            )
        return
    if not isinstance(source, Graph):
        raise InputError("Queries require a graph or prepared source")
    is_dataset = isinstance(source, (Dataset, ConjunctiveGraph))
    if scope.kind == "named":
        if isinstance(scope.graph_name, BNode):
            raise InputError("Blank graph names require an owned PreparedSource")
        if not is_dataset:
            raise InputError("Named query scope requires a dataset")
        assert isinstance(source, (Dataset, ConjunctiveGraph))
        contexts = source.graphs() if isinstance(source, Dataset) else source.contexts()
        for graph in contexts:
            budget.consume("work")
            budget.consume("states")
            if graph.identifier == scope.graph_name:
                yield None, graph
                return
        raise InputError("Selected graph is absent from the supplied dataset")
    if not is_dataset:
        yield None, source
        return
    assert isinstance(source, (Dataset, ConjunctiveGraph))
    default = (
        source.default_graph if isinstance(source, Dataset) else source.default_context
    )
    if scope.kind == "default":
        yield None, default
        return
    contexts = source.graphs() if isinstance(source, Dataset) else source.contexts()
    for graph in contexts:
        budget.consume("work")
        budget.consume("states")
        yield (
            None
            if scope.kind == "union" or graph.identifier == default.identifier
            else cast(URIRef | BNode, graph.identifier),
            graph,
        )


def _request(
    source: Graph | PreparedSource,
    text: str,
    scope: QueryScope,
    max_rows: int,
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> bytes:
    """Capture bounded typed input, including empty named graph inventory."""
    budget.consume("bytes", len(text.encode()))
    records = []
    namespaces: dict[str, str] = {}
    for name, graph in _graphs(source, scope, budget):
        checkpoint(deadline, cancel)
        budget.consume("resources")
        rows = []
        size = 0
        for triple in graph:
            checkpoint(deadline, cancel)
            budget.consume("work")
            budget.consume("triples")
            validate_triple(*triple)
            row = [term_data(term) for term in triple]
            encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()
            size += len(encoded)
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Query input graph exceeds max_bytes")
            budget.consume("bytes", len(encoded))
            rows.append(row)
        if name is not None and not isinstance(name, (URIRef, BNode)):
            raise InputError("Query graph names must be IRIs or owned blanks")
        encoded_name = term_data(name)
        budget.consume("bytes", len(json.dumps(encoded_name).encode()))
        records.append((encoded_name, rows))
        for prefix, iri in graph.namespaces():
            budget.consume("work")
            namespace = absolute_iri(str(iri))
            if prefix in namespaces and namespaces[prefix] != namespace:
                raise InputError(
                    "Query namespace prefix has conflicting supplied bindings"
                )
            if prefix not in namespaces:
                budget.consume("bytes", len(prefix.encode()) + len(namespace.encode()))
                namespaces[prefix] = namespace
    request = {
        "query": text,
        "graphs": records,
        "namespaces": list(namespaces.items()),
        "dataset": scope.kind == "dataset",
        "max_rows": max_rows,
        "limits": asdict(budget.limits),
        "output_cap": budget.limits.max_total_bytes,
    }
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    remaining = budget.limits.max_total_bytes - len(payload)
    if remaining < 256:
        raise LimitExceeded("Local query input leaves no bounded response allowance")
    request["output_cap"] = remaining
    return json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()


def _result(
    raw: bytes,
    scope: QueryScope,
    limits: Limits,
    max_rows: int,
    *,
    budget: Budget | None = None,
) -> QueryResult:
    """Validate peer output before constructing a completed typed public answer."""
    response = decode_json(
        raw, max_bytes=limits.max_total_bytes, max_depth=limits.max_depth
    )
    if not isinstance(response, dict):
        raise ResourceError("Invalid local query response object")
    status = response.get("status")
    if status == "unverified":
        if set(response) != {"status", "message"} or not isinstance(
            response["message"], str
        ):
            raise ResourceError("Malformed query literal-scope response")
        return QueryResult(
            Report(
                "rdf.query",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "rdf.literal_conversion_unverified",
                        response["message"],
                        kind="unverified",
                    ),
                ),
            ),
            scope,
        )
    if status != "result":
        if not isinstance(status, str) or status not in {"input_error", "exhausted"}:
            raise ResourceError("Unrecognised local query response status")
        error = (
            LimitExceeded(str(response.get("message", "Query incomplete")))
            if status == "exhausted"
            else InputError(str(response.get("message", "Query input invalid")))
        )
        raise error
    kind, complete = response.get("kind"), response.get("complete")
    if (
        not isinstance(kind, str)
        or kind not in {"ASK", "SELECT"}
        or type(complete) is not bool
    ):
        raise ResourceError("Invalid local query completion evidence")
    if kind == "ASK":
        if set(response) != {"status", "kind", "answer", "complete"}:
            raise ResourceError("Invalid local ASK response fields")
        if type(response.get("answer")) is not bool or not complete:
            raise ResourceError("Invalid local ASK answer")
        return QueryResult(
            Report("rdf.query", 1, requested=1, families=("sparql-ask",)),
            scope,
            "ASK",
            response["answer"],
        )
    if set(response) != {"status", "kind", "variables", "rows", "complete", "message"}:
        raise ResourceError("Invalid local SELECT response fields")
    message = response["message"]
    if (
        not isinstance(message, str)
        or (complete and message)
        or (not complete and not message)
    ):
        raise ResourceError("Invalid local SELECT completion reason")
    variables, supplied = response.get("variables"), response.get("rows")
    if (
        not isinstance(variables, list)
        or len(variables) > limits.max_states
        or any(not isinstance(v, str) or not v for v in variables)
        or len(set(variables)) != len(variables)
    ):
        raise ResourceError("Invalid local SELECT variables")
    if not isinstance(supplied, list) or len(supplied) > max_rows:
        raise ResourceError("Invalid local SELECT row scope")
    blanks: dict[str, BNode] = {}
    rows = []
    budget = budget if budget is not None else Budget(limits)
    budget.consume("states", len(variables))
    for row in supplied:
        budget.consume("states", len(variables))
        if not isinstance(row, list) or len(row) != len(variables):
            raise ResourceError("Invalid local SELECT row width")
        rows.append(tuple(read_term(value, blanks) for value in row))
    issues = (
        ()
        if complete
        else (
            Issue(
                "execution.limit_exceeded",
                str(response.get("message", "SELECT result incomplete")),
                kind="exhausted",
            ),
        )
    )
    return QueryResult(
        Report(
            "rdf.query",
            1,
            issues,
            requested=1,
            complete=complete,
            families=("sparql-select",),
        ),
        scope,
        "SELECT",
        variables=tuple(variables),
        rows=tuple(rows),
    )


def query_rdf(
    source: Graph | PreparedSource,
    text: str,
    *,
    scope: QueryScope,
    max_rows: int = 1000,
    limits: Limits | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
    _owned_worker: bool = False,
) -> QueryResult:
    """Run local ASK/SELECT with explicit graph scope and an owned deadline.

    Parser and query evaluation run in a disposable process. SERVICE and every
    FROM clause are prohibited using the maintained parse tree. Limits bound
    capture, worker parsing/evaluation, and reply validation independently;
    counters are local to those stages, with one operation-wide deadline.
    Worker work counts input records, syntax and retained solutions; opaque
    engine search uses the real deadline, not an instruction counter. Namespace
    bindings come only from supplied selected graphs or explicit query PREFIX.
    """
    if not isinstance(scope, QueryScope):
        raise InputError("Queries require an explicit QueryScope")
    if not isinstance(text, str) or not text.strip():
        raise InputError("Queries require nonempty SPARQL text")
    if type(max_rows) is not int or max_rows <= 0:
        raise InputError("max_rows must be a positive integer")
    if type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
        raise InputError("Query timeout must be finite and positive")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Query cancellation requires a threading.Event")
    selected = limits if limits is not None else Limits()
    deadline = time.monotonic() + timeout
    budget = _CaptureBudget(selected, deadline, cancel)
    try:
        checkpoint(deadline, cancel)
        if len(text.encode()) > selected.max_bytes:
            raise LimitExceeded("SPARQL text exceeds max_bytes")
        payload = _request(source, text, scope, max_rows, budget, deadline, cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("RDF operation exceeded its wall-clock deadline")
        if _owned_worker:
            # The fixed project worker's host owns cancellation and its absolute
            # job deadline, including opaque native search. A second process
            # group would escape that ownership when the project is stopped.
            from bijux_ontology.rdf.queries.worker import process_request

            raw = process_request(payload, selected.max_total_bytes)
        else:
            raw = execute_worker(
                "bijux_ontology.rdf.queries.worker",
                payload,
                cap=selected.max_total_bytes,
                timeout=remaining,
                cancel=cancel,
            )
        budget.consume("bytes", len(raw))
        result = _result(raw, scope, selected, max_rows)
        checkpoint(deadline, cancel)
        if (
            isinstance(source, PreparedSource)
            and scope.kind == "dataset"
            and source.graph_fidelity != "complete"
        ):
            from dataclasses import replace

            return replace(
                result,
                report=Report(
                    "rdf.query",
                    1,
                    result.report.issues
                    + (
                        Issue(
                            "rdf.graph_scope_unverified",
                            "Input does not establish complete empty-graph inventory",
                            kind="unverified",
                        ),
                    ),
                    requested=1,
                    complete=False,
                    families=result.report.families,
                ),
            )
        return result
    except (InputError, ResourceError) as error:
        kind: IssueKind = (
            "exhausted" if isinstance(error, LimitExceeded) else "input_error"
        )
        return QueryResult(
            Report(
                "rdf.query",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "execution.limit_exceeded"
                        if kind == "exhausted"
                        else "input.invalid",
                        str(error),
                        kind=kind,
                    ),
                ),
            ),
            scope,
        )
