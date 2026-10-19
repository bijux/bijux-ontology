"""Joint maintained RDF comparison with source-root annotation attribution."""

import threading
import time
from typing import cast

from rdflib import BNode, Dataset, Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.obo.types import OboAttachment, OboRecord

from .reading import _bound, _same, inspect_annotation_graph
from .types import (
    AnnotationGraph,
    AnnotationGraphComparison,
    AnnotationResource,
    AnnotationStatement,
    AnnotationValue,
)


def _projection(
    selected: AnnotationGraph, budget: Budget, root: AnnotationStatement | None = None
) -> Graph:
    """Retain original assertions only; no synthetic owner links or blank remapping."""
    dataset = Dataset() if selected.graph is not None else None
    graph = (
        dataset.graph(selected.graph)
        if dataset is not None
        else Graph(bind_namespaces="none")
    )
    nodes: set[AnnotationResource] = set()
    if root is not None:
        for path in selected.paths:
            budget.consume("work")
            if (
                _same(path.root.subject, root.subject)
                and path.root.predicate == root.predicate
                and _same(path.root.value, root.value)
            ):
                budget.consume("states", len(path.attachments))
                nodes.update(path.attachments)
    for statement in selected.statements:
        budget.consume("work")
        if root is not None and statement != root and statement.subject not in nodes:
            continue
        _bound(statement, budget)
        budget.consume("triples")
        budget.consume("states")
        graph.add((statement.subject, statement.predicate, statement.value))
    return dataset if dataset is not None else graph


def _compare(before: Graph, after: Graph, operation: Operation) -> Report:
    """Use the maintained comparator within the original remaining deadline."""
    operation.consume("work")
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Annotation comparison exceeded its deadline")
    return compare_rdf(before, after, budget=operation, timeout_seconds=remaining)


def _key(root: AnnotationStatement) -> tuple[tuple[str | None, ...], ...]:
    """Associate named roots through existing intrinsic RDF term identity."""
    return tuple(term_key(node) for node in (root.subject, root.predicate, root.value))


def compare_annotation_graphs(
    before: AnnotationGraph,
    after: AnnotationGraph,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> AnnotationGraphComparison:
    """Compare joint blank sharing and attribute changes to exact named root facts.

    A fresh bounded read qualifies public input records against their immutable
    sources. Independent anonymous roots and changed root identities have no
    guessed correspondence. Joint sharing changes without a unique local owner
    retain RDF disagreement and explicitly uncertain root attribution.
    """
    if not isinstance(before, AnnotationGraph) or not isinstance(
        after, AnnotationGraph
    ):
        raise InputError("Annotation comparison requires two typed graph selections")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children = []
    changed = []
    uncertain = []
    checked = 0
    try:
        before = inspect_annotation_graph(
            before.source,
            before.roots,
            graph=before.graph,
            annotation_predicates=before.annotation_predicates,
            budget=operation,
        )
        after = inspect_annotation_graph(
            after.source,
            after.roots,
            graph=after.graph,
            annotation_predicates=after.annotation_predicates,
            budget=operation,
        )
        for selected in (before, after):
            retain_report(selected.report, operation, issues)
            children.append(selected.report)
        joint = _compare(
            _projection(before, operation),
            _projection(after, operation),
            operation,
        )
        retain_report(joint, operation, issues)
        children.append(joint)
        operation.consume("states", len(after.roots))
        remaining = {_key(root) for root in after.roots}
        for root in before.roots:
            operation.consume("work")
            _bound(root, operation)
            if (
                isinstance(root.subject, BNode)
                or isinstance(root.value, BNode)
                or _key(root) not in remaining
            ):
                operation.consume("states")
                uncertain.append(root)
                continue
            remaining.remove(_key(root))
            result = _compare(
                _projection(before, operation, root),
                _projection(after, operation, root),
                operation,
            )
            retain_report(result, operation, issues)
            children.append(result)
            if result.outcome == "disagreement":
                operation.consume("states")
                changed.append(root)
            elif not result.verified:
                operation.consume("states")
                uncertain.append(root)
            checked += 1
        if remaining or uncertain or (joint.outcome == "disagreement" and not changed):
            if not uncertain:
                operation.consume("states", len(before.roots))
                uncertain.extend(before.roots)
            issues.append(
                Issue(
                    "annotations.comparison_unverified",
                    "Annotation changes lack a unique selected-root correspondence",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return AnnotationGraphComparison(
        tuple(changed),
        tuple(uncertain),
        before,
        after,
        Report(
            "annotations.comparison",
            checked,
            tuple(issues),
            requested=max(1, len(before.roots)),
            allow_empty=True,
            children=tuple(children),
            regime="exact-annotation-rdf",
        ),
    )


def adapt_obo_record(
    graph: AnnotationGraph, record: OboRecord, *, budget: Budget | None = None
) -> AnnotationGraph:
    """Select a real OBO record through its original assertion and attachment terms.

    The adapter interprets no new OBO dialect and never merges equal synonym
    forms. Blank attachment correspondence must be the actual owned node object;
    a record from a separately copied vocabulary is deliberately unsupported.
    """
    if not isinstance(graph, AnnotationGraph) or not isinstance(record, OboRecord):
        raise InputError("Expected an annotation graph and an existing OBO record")
    operation = Operation(None, budget, 30.0, None)
    root = AnnotationStatement(
        cast(AnnotationResource, record.assertion.subject),
        record.assertion.predicate,
        cast(AnnotationValue, record.assertion.object),
    )
    selected = inspect_annotation_graph(
        graph.source,
        (root,),
        graph=graph.graph,
        annotation_predicates=graph.annotation_predicates,
        budget=operation,
    )
    issues = IssueCollector(operation)
    try:
        retain_report(selected.report, operation, issues)
        if not record.complete:
            issues.append(
                Issue(
                    "annotations.graph_unverified",
                    "OBO record has an incomplete original assertion inventory",
                    kind="unverified",
                )
            )
        attachments = capture_items(record.attachments, OboAttachment, operation)
        matched: set[int] = set()
        for attachment in attachments:
            operation.consume("work")
            operation.consume("states")
            found = False
            for actual in selected.attachments:
                operation.consume("work")
                if (
                    actual.node is attachment.node
                    if isinstance(attachment.node, BNode)
                    else actual.node == attachment.node
                ):
                    found = (
                        actual.annotated == root
                        and actual.status == "attached"
                        and id(actual) not in matched
                    )
                    break
            if found:
                operation.consume("states", len(attachment.statements))
                found = (
                    attachment.complete
                    and attachment.status == "attached"
                    and len(attachment.statements) == len(actual.statements)
                )
                witnessed_edges: set[int] = set()
                for edge in attachment.statements:
                    operation.consume("work")
                    original = AnnotationStatement(
                        cast(AnnotationResource, edge.subject),
                        edge.predicate,
                        cast(AnnotationValue, edge.object),
                    )
                    _bound(original, operation)
                    witnessed = False
                    for position, statement in enumerate(actual.statements):
                        operation.consume("work")
                        _bound(statement, operation)
                        if (
                            position not in witnessed_edges
                            and _same(original.subject, statement.subject)
                            and original.predicate == statement.predicate
                            and _same(original.value, statement.value)
                        ):
                            operation.consume("states")
                            witnessed_edges.add(position)
                            witnessed = True
                            break
                    found &= witnessed
            if found:
                matched.add(id(actual))
            if not found:
                issues.append(
                    Issue(
                        "annotations.graph_unverified",
                        "OBO attachment lacks its exact original graph witness",
                        kind="unverified",
                    )
                )
        for actual in selected.attachments:
            operation.consume("work")
            if actual.annotated == root and id(actual) not in matched:
                issues.append(
                    Issue(
                        "annotations.graph_unverified",
                        "OBO record omits an original root attachment",
                        kind="unverified",
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    from dataclasses import replace

    return replace(
        selected,
        report=Report(
            "annotations.obo",
            1,
            tuple(issues),
            children=(selected.report,),
            regime="native-obo-attachment",
        ),
    )
