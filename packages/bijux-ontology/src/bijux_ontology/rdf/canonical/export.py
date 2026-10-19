"""Canonical RDF 1.1 quad artifacts with owned execution and qualified fidelity."""

import json
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from math import isfinite

import pyoxigraph as ox
from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, BudgetKind, IssueCollector, Limits
from bijux_ontology.execution.processes.transport import checkpoint
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import term_key, validate_triple


@dataclass(frozen=True)
class CanonicalRDF:
    """Owned canonical bytes for selected quads, never persistent entity identity."""

    content: bytes | None
    report: Report
    hash_algorithm: str
    quad_count: int
    literal_fidelity: str
    algorithm: str = "RDFC-1.0"
    rdf_version: str = "1.1"
    backend: str | None = None
    quad_scope: str = "graph"
    graph_name: str | None = None


class _CanonicalBudget(Budget):
    """Keep metadata walks and term capture inside the operation deadline."""

    def __init__(
        self, shared: Budget, deadline: float, cancel: threading.Event | None
    ) -> None:
        super().__init__(shared.limits)
        self.shared = shared
        self.deadline, self.cancel = deadline, cancel

    def remaining(self, kind: BudgetKind) -> int:
        """Read the caller's allowance without granting fresh metadata capacity."""
        return self.shared.remaining(kind)

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        checkpoint(self.deadline, self.cancel)
        self.shared.consume(kind, amount)


def _graphs(
    source: Graph | PreparedSource, budget: Budget
) -> Iterator[tuple[URIRef | BNode | None, Graph]]:
    """Capture selected roles without copying unneeded namespace metadata."""
    if isinstance(source, PreparedSource):
        budget.consume("resources")
        if not source.is_dataset or source.graph_name is not None or source.union:
            yield None, source._read(budget=budget)
            return
        yield None, source._read("@default", budget=budget)
        for name in source._graph_names(budget=budget):
            budget.consume("resources")
            yield name, source._read(name, budget=budget)
        return
    if not isinstance(source, Graph):
        raise InputError("Canonical RDF requires a graph or prepared source")
    if not isinstance(source, Dataset | ConjunctiveGraph):
        budget.consume("resources")
        yield None, source
        return
    default = (
        source.default_graph if isinstance(source, Dataset) else source.default_context
    )
    graphs = source.graphs() if isinstance(source, Dataset) else source.contexts()
    for graph in graphs:
        budget.consume("resources")
        budget.consume("work")
        budget.consume("states")
        raw_name = graph.identifier
        if not isinstance(raw_name, URIRef | BNode):
            raise InputError("Canonical RDF graph names must be IRIs or blanks")
        yield None if raw_name == default.identifier else raw_name, graph


def _records(
    source: Graph | PreparedSource,
    budget: Budget,
    issues: IssueCollector,
    ignore_empty_graphs: bool,
) -> tuple[list[list[tuple[str | None, ...]]], str]:
    """Retain exact terms under one safe dataset-wide blank-node mapping."""
    fidelity = (
        source.literal_fidelity
        if isinstance(source, PreparedSource)
        else "supplied_terms"
    )
    if fidelity not in {"source_terms", "supplied_terms"}:
        issues.append(
            Issue(
                "rdf.canonicalization_literal_scope_unverified",
                "Input does not establish exact supported literal terms",
                kind="unverified",
                actual=fidelity,
            )
        )
        return [], fidelity
    whole = (
        isinstance(source, PreparedSource)
        and source.is_dataset
        and source.graph_name is None
        and not source.union
    )
    if (
        isinstance(source, PreparedSource)
        and whole
        and source.graph_fidelity != "complete"
        and not ignore_empty_graphs
    ):
        issues.append(
            Issue(
                "rdf.canonicalization_graph_scope_unverified",
                "Input does not establish complete empty named-graph inventory",
                kind="unverified",
            )
        )
        return [], fidelity
    blanks: dict[BNode, str] = {}

    def term(node: Node | None) -> tuple[str | None, ...]:
        """Use legal internal labels while retaining exact RDF literal fields."""
        if isinstance(node, BNode):
            if node not in blanks:
                budget.consume("states")
                blanks[node] = f"b{len(blanks)}"
            return "blank", blanks[node]
        return term_key(node)

    records = []
    for name, graph in _graphs(source, budget):
        if name is not None and not len(graph) and not ignore_empty_graphs:
            issues.append(
                Issue(
                    "rdf.canonicalization_empty_graph_loss",
                    "Canonical N-Quads cannot retain an empty named graph",
                )
            )
            return [], fidelity
        size = 0
        for subject, predicate, value in graph:
            budget.consume("work")
            budget.consume("triples")
            validate_triple(subject, predicate, value)
            row = [term(node) for node in (subject, predicate, value, name)]
            encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()
            size += len(encoded)
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Canonical RDF input graph exceeds max_bytes")
            budget.consume("bytes", len(encoded))
            records.append(row)
    return records, fidelity


def _validate_artifact(content: bytes, count: int, budget: Budget) -> None:
    """Check maintained N-Quads syntax and the selected canonical RDF profile."""
    lines: list[bytes] = []
    blanks: set[str] = set()
    try:
        for quad in ox.parse(input=content, format=ox.RdfFormat.N_QUADS):
            budget.consume("work")
            if len(lines) >= count:
                raise ResourceError("Canonical RDF artifact exceeds its quad scope")
            if (
                not isinstance(quad.subject, ox.NamedNode | ox.BlankNode)
                or not isinstance(quad.predicate, ox.NamedNode)
                or not isinstance(quad.object, ox.NamedNode | ox.BlankNode | ox.Literal)
                or not isinstance(
                    quad.graph_name, ox.NamedNode | ox.BlankNode | ox.DefaultGraph
                )
            ):
                raise ResourceError(
                    "Canonical artifact contains unsupported RDF 1.1 terms"
                )
            for node in quad:
                if isinstance(node, ox.Literal) and node.direction is not None:
                    raise ResourceError(
                        "Canonical artifact contains directional literals"
                    )
                if isinstance(node, ox.BlankNode):
                    if re.fullmatch(r"c14n(?:0|[1-9][0-9]*)", node.value) is None:
                        raise ResourceError(
                            "Canonical artifact has noncanonical blank labels"
                        )
                    if node.value not in blanks:
                        budget.consume("states")
                        blanks.add(node.value)
            line = ox.serialize((quad,), format=ox.RdfFormat.N_QUADS)
            if line is None:
                raise ResourceError("Canonical artifact could not be serialized")
            lines.append(line)
    except InputError:
        raise
    except (SyntaxError, ValueError, UnicodeError) as error:
        raise ResourceError("Canonical worker returned invalid N-Quads") from error
    if (
        len(lines) != count
        or len(set(lines)) != count
        or content != b"".join(sorted(lines))
        or blanks != {f"c14n{index}" for index in range(len(blanks))}
    ):
        raise ResourceError(
            "Canonical worker returned an invalid canonical serialization"
        )


def _content(
    raw: bytes, hash_algorithm: str, count: int, budget: Budget
) -> tuple[bytes, str]:
    """Accept only a complete bounded result from the selected standard profile."""
    budget.consume("bytes", len(raw))
    response = decode_json(
        raw, max_bytes=budget.limits.max_total_bytes, max_depth=budget.limits.max_depth
    )
    if not isinstance(response, dict):
        raise ResourceError("Invalid canonical RDF worker response")
    status = response.get("status")
    if status == "exhausted":
        raise LimitExceeded(str(response.get("message", "Canonical RDF exhausted")))
    if status == "unavailable":
        raise ResourceError(str(response.get("message", "Canonical RDF unavailable")))
    if status == "input_error":
        raise InputError(str(response.get("message", "Invalid canonical RDF input")))
    if (
        status != "result"
        or response.get("algorithm") != "RDFC-1.0"
        or response.get("hash_algorithm") != hash_algorithm
        or response.get("rdf_version") != "1.1"
        or type(response.get("quad_count")) is not int
        or response["quad_count"] != count
        or not isinstance(response.get("backend"), str)
        or not response["backend"].startswith("pyoxigraph/")
        or not isinstance(response.get("content"), str)
    ):
        raise ResourceError("Invalid canonical RDF completion evidence")
    content = response["content"].encode("utf-8")
    if len(content) > budget.limits.max_bytes:
        raise LimitExceeded("Canonical RDF artifact exceeds max_bytes")
    _validate_artifact(content, count, budget)
    budget.consume("bytes", len(content))
    return content, response["backend"]


def canonicalize_rdf(
    source: Graph | PreparedSource,
    *,
    hash_algorithm: str = "SHA256",
    ignore_empty_graphs: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
    memory_bytes: int | None = None,
    cancel: threading.Event | None = None,
) -> CanonicalRDF:
    """Publish RDFC-1.0 canonical N-Quads for an explicit RDF 1.1 quad scope.

    SHA256 and SHA384 select the standard's internal hash algorithm. Bytes are
    not a digest, an OWL entailment result, or stable labels across changed data.
    Empty named graphs and uncertain literal preservation prevent publication;
    ignore_empty_graphs explicitly excludes graph inventory from the claim.
    Earlier caller normalization is unrecoverable for supplied RDFLib terms.

    Input capture and retained output consume the operation Budget. A disposable
    maintained backend has an actual deadline because its exponential search has
    no exposed instruction allowance. Requested OS memory limits fail explicitly
    on unsupported platforms; finite input/output limits are not a hard RSS cap.
    """
    if hash_algorithm not in ("SHA256", "SHA384"):
        raise InputError("Canonical RDF hash_algorithm must be SHA256 or SHA384")
    if type(ignore_empty_graphs) is not bool:
        raise InputError("ignore_empty_graphs must be a boolean")
    if (
        type(timeout_seconds) not in (int, float)
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise InputError("Canonical RDF timeout must be finite and positive")
    if memory_bytes is not None and (
        type(memory_bytes) is not int or memory_bytes <= 0
    ):
        raise InputError("memory_bytes must be a positive integer")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Canonical RDF cancellation requires a threading.Event")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    selected = limits if limits is not None else budget.limits if budget else Limits()
    deadline = time.monotonic() + timeout_seconds
    shared = budget or Budget(selected)
    operation = _CanonicalBudget(shared, deadline, cancel)
    issues = IssueCollector(shared)
    content, backend, fidelity = None, None, "unestablished"
    count = 0
    exclusions = ("empty-named-graphs",) if ignore_empty_graphs else ()
    graph_name = source.graph_name if isinstance(source, PreparedSource) else None
    quad_scope = (
        "union"
        if isinstance(source, PreparedSource) and source.union
        else "default"
        if graph_name == "@default"
        else "named"
        if graph_name is not None
        else "dataset"
        if isinstance(source, Dataset | ConjunctiveGraph)
        or isinstance(source, PreparedSource)
        and source.is_dataset
        else "graph"
    )
    try:
        checkpoint(deadline, cancel)
        records, fidelity = _records(source, operation, issues, ignore_empty_graphs)
        count = len(records)
        if not issues:
            payload = json.dumps(
                {
                    "quads": records,
                    "limits": vars(selected),
                    "hash_algorithm": hash_algorithm,
                    "memory_bytes": memory_bytes,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
            operation.consume("bytes", len(payload))
            checkpoint(deadline, cancel)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Canonical RDF exceeded its wall-clock deadline")
            raw = execute_worker(
                "bijux_ontology.rdf.canonical.worker",
                payload,
                cap=selected.max_total_bytes,
                timeout=remaining,
                cancel=cancel,
            )
            content, backend = _content(raw, hash_algorithm, count, operation)
            checkpoint(deadline, cancel)
    except LimitExceeded as error:
        issues.exhausted(str(error))
        content = None
    except (InputError, UnicodeError) as error:
        try:
            issues.append(
                Issue(
                    "rdf.canonicalization_unavailable"
                    if isinstance(error, ResourceError)
                    else "rdf.canonicalization_input_invalid",
                    str(error),
                    kind="input_error",
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
        content = None
    return CanonicalRDF(
        content,
        Report(
            "rdf.canonicalization",
            int(content is not None),
            tuple(issues),
            requested=1,
            complete=content is not None,
            regime="rdfc-1.0-rdf-1.1-quads",
            families=("rdf.canonicalization",),
            exclusions=exclusions,
        ),
        hash_algorithm,
        count,
        fidelity,
        backend=backend,
        quad_scope=quad_scope,
        graph_name=graph_name,
    )
