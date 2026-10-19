"""Atomic selected root-list edits with explicit sequence and application intent."""

import threading
import time
from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.execution import checkpoint
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import Captured, Rejected, capture_source
from bijux_ontology.rdf.patches.types import (
    PatchBlankBinding,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report

from .planning import copy_chain
from .types import (
    ListInsert,
    ListMigrationCandidate,
    ListRemove,
    ListReorder,
    ListTarget,
    ListValue,
    _index,
    _indices,
    _terms,
)


def _portable(value: ListValue, captured: Captured, budget: Budget) -> PatchValue:
    """Preserve original literal identity and require actual owned blank terms."""
    bound_node(value, budget)
    if isinstance(value, BNode):
        return captured.reference(value)
    if isinstance(value, Literal):
        return PatchLiteral(str(value), str(literal_datatype(value)), value.language)
    return URIRef(value)


def _scope(
    source: PreparedSource, target: ListTarget, captured: Captured, budget: Budget
) -> tuple[Graph, PatchQuad]:
    """Resolve one physical incoming edge without widening source authorization."""
    subject = cast(PatchResource, _portable(target.subject, captured, budget))
    head = cast(PatchResource, _portable(target.head, captured, budget))
    bound_node(target.predicate, budget)
    if source.is_dataset and target.graph is ABSENT:
        raise PatchUnavailable("Dataset list edits require an explicit physical graph")
    graph = target.graph if target.graph is not ABSENT else None
    graph_ref = (
        cast(PatchResource, _portable(graph, captured, budget))
        if graph is not None
        else None
    )
    if (
        captured.witness.kind == "named"
        and graph_ref != captured.witness.graph_selection
    ):
        raise PatchUnavailable("The explicit graph conflicts with the pinned source")
    if not source.is_dataset and graph is not None:
        raise PatchUnavailable("A graph input cannot authorize a named graph edit")
    budget.consume("work")
    if graph_ref is not None and graph_ref not in captured.witness.graphs:
        raise PatchUnavailable("The selected list graph is outside the owned scope")
    actual = source._read(
        (graph if graph is not None else "@default") if source.is_dataset else None,
        budget=budget,
    )
    incoming = PatchQuad(subject, target.predicate, head, graph_ref)
    budget.consume("work", len(captured.witness.quads))
    if incoming not in captured.witness.quads:
        raise Rejected(
            "rdf.list_migration_rejected", "The exact selected incoming edge is absent"
        )
    budget.consume("work", 3)
    if target.predicate in (RDF.first, RDF.rest):
        raise PatchUnavailable("List edits require an independent root incoming edge")
    for predicate in (RDF.first, RDF.rest):
        budget.consume("work")
        if next(actual.triples((target.subject, predicate, None)), None) is not None:
            raise PatchUnavailable(
                "A list-cell owner cannot establish independent root ownership"
            )
    return actual, incoming


def _edit(
    before: tuple[PatchValue, ...],
    action: ListInsert | ListRemove | ListReorder,
    captured: Captured,
    budget: Budget,
) -> tuple[PatchValue, ...]:
    """Apply finite positional intent without interpreting RDF member values."""
    if isinstance(action, ListInsert):
        _index(action.index, budget)
        values = tuple(
            _portable(value, captured, budget)
            for value in _terms(action.values, budget)
        )
        if action.index > len(before):
            raise Rejected(
                "rdf.list_migration_rejected", "Insertion index is out of range"
            )
        budget.consume("states", len(before) + len(values))
        budget.consume("work", len(before) + len(values))
        return before[: action.index] + values + before[action.index :]
    indices = _indices(action.indices, budget)
    budget.consume("work", len(before) + len(indices))
    if any(index >= len(before) for index in indices):
        raise Rejected("rdf.list_migration_rejected", "Selected index is out of range")
    if isinstance(action, ListReorder):
        if len(indices) != len(before):
            raise Rejected(
                "rdf.list_migration_rejected",
                "Reorder must select every original position",
            )
        budget.consume("states", len(before))
        return tuple(before[index] for index in indices)
    budget.consume("states", len(indices) + len(before))
    removed = set(indices)
    return tuple(value for index, value in enumerate(before) if index not in removed)


def migrate_list(
    source: PreparedSource,
    target: ListTarget,
    action: ListInsert | ListRemove | ListReorder,
    *,
    expected_before: tuple[ListValue, ...],
    expected_after: tuple[ListValue, ...],
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> ListMigrationCandidate:
    """Copy a selected root list only after exact before/after intent is established.

    A changed list gets a wholly fresh chain. Original cells, annotations and
    sibling incoming edges stay untouched, so the selected chain's sharing may
    intentionally change. Anonymous target/member terms require source ownership.
    Every dataset target names its physical graph explicitly. Expected sequences
    compare RDF terms, never Python numeric values or normalized literal spellings.
    Ordinary selected application checks and explicit accepted export use the
    established migration gates; missing after checks never yield verified export.
    """
    if not isinstance(target, ListTarget) or not isinstance(
        action, ListInsert | ListRemove | ListReorder
    ):
        raise InputError("List migrations require a typed target and positional action")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    patch = None
    transformed = None
    before: tuple[PatchValue, ...] = ()
    after: tuple[PatchValue, ...] = ()
    copied = 0
    checked = 0
    try:
        captured = capture_source(source, "base:", operation)
        before_intent = tuple(
            _portable(value, captured, operation)
            for value in _terms(expected_before, operation)
        )
        after_intent = tuple(
            _portable(value, captured, operation)
            for value in _terms(expected_after, operation)
        )
        graph, incoming = _scope(source, target, captured, operation)
        checked += 1
        values: list[PatchValue] = []
        try:
            for value in iter_rdf_list(
                graph,
                target.head,
                max_items=operation.limits.max_states,
                budget=operation,
            ):
                operation.consume("states")
                values.append(_portable(cast(ListValue, value), captured, operation))
        except LimitExceeded:
            raise
        except InputError as error:
            raise Rejected(
                "rdf.list_migration_rejected", "The selected RDF list is malformed"
            ) from error
        before = tuple(values)
        checked += 1
        if before != before_intent:
            raise Rejected(
                "rdf.list_migration_rejected",
                "Original list differs from expected sequence",
            )
        after = _edit(before, action, captured, operation)
        checked += 1
        if after != after_intent:
            raise Rejected(
                "rdf.list_migration_rejected",
                "Requested edit differs from expected sequence",
            )
        patch = copy_chain(captured.witness, incoming, before, after, operation)
        copied = len(after) if before != after else 0
        bindings: list[PatchBlankBinding] = []
        seen: set[str] = set()
        for node, reference in captured.owned.values():
            operation.consume("work")
            if reference.token not in seen:
                operation.consume("states")
                seen.add(reference.token)
                bindings.append(PatchBlankBinding(reference.token, node))
        checkpoint(operation.deadline, cancel)
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("List migration exceeded its deadline")
        transformed = apply_rdf_patch(
            source,
            patch,
            blank_bindings=tuple(bindings),
            budget=operation,
            timeout=remaining,
            cancel=cancel,
        )
        issues.extend(transformed.report.issues)
        children.append(transformed.report)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (Rejected, PatchUnavailable) as error:
        try:
            issues.append(
                Issue(
                    error.code
                    if isinstance(error, Rejected)
                    else "rdf.list_migration_unverified",
                    str(error),
                    kind="finding" if isinstance(error, Rejected) else "unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    raw = Report(
        "rdf.list_migration",
        checked,
        tuple(issues),
        requested=max(1, checked),
        allow_empty=True,
        children=tuple(children),
        regime="exact-root-list-copy-on-write",
    )
    finished = finish_migration(
        source,
        Transformation(
            transformed.source if transformed is not None and raw.verified else None,
            raw,
        ),
        patch,
        before_checks=before_checks,
        after_checks=after_checks,
        budget=operation,
    )
    return ListMigrationCandidate(
        finished.source,
        finished.patch,
        finished.before,
        finished.after,
        finished.report,
        len(before),
        len(after),
        copied,
        _metadata=finished._metadata,
        _checked_source=finished._checked_source,
    )
