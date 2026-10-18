"""RDF structure identity through an isolated, bounded maintained comparator."""

import json
import math
import subprocess
import sys
from collections.abc import Iterator
from typing import cast

from rdflib import BNode, Graph, URIRef

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.reports import Issue, Report

_Record = tuple[str, tuple[tuple[str | None, ...], ...]]


def _contexts(
    source: PreparedSource, budget: Budget
) -> Iterator[tuple[URIRef | BNode | None, Graph]]:
    """Retain the authorized projection without exporting unrelated storage."""
    if source.union:
        yield None, source._read(budget=budget)
    elif source.graph_name is not None or not source.is_dataset:
        for _, name, graph in source._contexts(budget=budget):
            yield name, graph
    else:
        default = source._read("@default", budget=budget).identifier
        for graph in source._owned_contexts(budget):
            yield (
                (
                    None
                    if graph.identifier == default
                    else cast(URIRef | BNode, graph.identifier)
                ),
                graph,
            )


def _records(
    source: Graph | PreparedSource, budget: Budget, ignore_empty_graphs: bool
) -> set[_Record]:
    prepared = (
        source
        if isinstance(source, PreparedSource)
        else prepare_rdf(source, budget=budget)
    )
    records: set[_Record] = set()
    for name, graph in _contexts(prepared, budget):
        budget.consume("work")
        if name is not None:
            bound_node(name, budget)
            if not ignore_empty_graphs or len(graph):
                budget.consume("states")
                records.add(("graph", (term_key(name),)))
        for triple in graph:
            budget.consume("work")
            budget.consume("triples")
            for node in triple:
                bound_node(node, budget)
            record = ("quad", tuple(term_key(node) for node in (*triple, name)))
            if record not in records:
                budget.consume("states")
                records.add(record)
    return records


def _isomorphic(payload: bytes, timeout_seconds: float) -> bool:
    """Keep the backend in its caller's process group and kill it on deadline."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",  # Isolation ignores the environment-based bytecode safeguard.
            "-m",
            "bijux_ontology.rdf._comparison_worker",
            str(len(payload)),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        output, _ = process.communicate(payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate(timeout=1)
        raise LimitExceeded(
            "RDF comparison exceeded its wall-clock deadline"
        ) from error
    if process.returncode != 0 or output not in (b"true\n", b"false\n"):
        raise InputError("RDF comparison worker did not return a valid result")
    return output == b"true\n"


def compare_rdf(
    before: Graph | PreparedSource,
    after: Graph | PreparedSource,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
    ignore_empty_graphs: bool = False,
) -> Report:
    """Compare exact RDF structure, dataset membership and blank-node identity.

    Pinned graph/default selections and explicit union projections are retained.
    Ground records compare directly; blank-node correspondence uses RDFLib's
    maintained isomorphism implementation in a disposable worker.
    Input and preparation work have explicit budgets; the comparator has a real
    wall-clock deadline because RDFLib exposes no internal work/state allowance.
    The worker inherits the caller's process group for enclosing CLI deadlines.
    Implicit RDF 1.1 string datatypes and language-tag case denote the same term.
    Numeric lexical forms stay distinct; OWL entailment is never evaluated.
    """
    if type(ignore_empty_graphs) is not bool:
        raise InputError("ignore_empty_graphs must be a boolean")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise InputError("Comparison timeout must be a finite positive number")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    complete = True
    checked = 0
    try:
        if not ignore_empty_graphs and any(
            isinstance(source, PreparedSource) and source.graph_fidelity != "complete"
            for source in (before, after)
        ):
            issues.append(
                Issue(
                    "rdf.graph_scope_unverified",
                    "Input does not establish complete empty-graph scope",
                    "rdf",
                    kind="unverified",
                )
            )
            return Report(
                "rdf.comparison",
                0,
                tuple(issues),
                allow_empty=True,
                requested=1,
                complete=False,
                regime="rdf-structure",
                families=("rdf.comparison",),
            )
        left = _records(before, operation, ignore_empty_graphs)
        right = _records(after, operation, ignore_empty_graphs)
        payload = json.dumps(
            [list(left), list(right)], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        operation.consume("bytes", len(payload))
        if len(left) != len(right):
            equal = False
        elif not any(
            node[0] == "blank"
            for records in (left, right)
            for _, nodes in records
            for node in nodes
        ):
            equal = set(left) == set(right)
        else:
            equal = _isomorphic(payload, timeout_seconds)
        checked = 1
        if not equal:
            issues.append(
                Issue(
                    "rdf.structure_changed",
                    "Supplied RDF structures are not isomorphic",
                    "rdf",
                    expected=f"{len(left)} selected records",
                    actual=f"{len(right)} selected records",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf")
        complete = False
    return Report(
        "rdf.comparison",
        checked,
        tuple(issues),
        requested=1,
        complete=complete,
        regime="rdf-nonempty-graph-structure"
        if ignore_empty_graphs
        else "rdf-structure",
        families=("rdf.comparison",),
        exclusions=("empty-named-graphs",) if ignore_empty_graphs else (),
    )
