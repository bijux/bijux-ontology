"""Explicit graph routing through checked portable dataset patches."""

import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCandidate,
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.planning import (
    Captured,
    Rejected,
    capture_source,
    report,
)
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlankBinding,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    SnapshotWitness,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation

from .types import GraphRoute, _resource


@dataclass(frozen=True)
class _Route:
    """One operation-owned route with exact finite membership sets."""

    target: PatchResource | None
    subjects: frozenset[PatchResource] | None
    predicates: frozenset[URIRef] | None


def _term(
    value: URIRef | BNode | None, captured: Captured, budget: Budget
) -> PatchResource | None:
    """Resolve only actual owned blanks, never coincident external labels."""
    if value is None:
        return None
    _resource(value, budget)
    return captured.reference(value) if isinstance(value, BNode) else URIRef(value)


def _inventory(
    values: Sequence[URIRef | BNode], captured: Captured, budget: Budget
) -> tuple[PatchResource, ...]:
    """Capture named graph edits once before membership and duplicate checks."""
    owned = capture_items(values, object, budget)
    result = []
    seen = set()
    for value in owned:
        if not isinstance(value, URIRef | BNode):
            raise InputError("Graph inventory edits require named resource terms")
        term = cast(PatchResource, _term(value, captured, budget))
        budget.consume("work")
        if term in seen:
            raise InputError("Graph inventory edits cannot repeat graphs")
        budget.consume("states", 2)
        seen.add(term)
        result.append(term)
    return tuple(result)


def _patch(
    captured: Captured,
    routes: tuple[GraphRoute, ...],
    added: tuple[PatchResource, ...],
    removed: tuple[PatchResource, ...],
    merge: bool,
    budget: Budget,
) -> DatasetPatch:
    """Route original quads once, preserving every untouched term and context."""
    base = captured.witness
    budget.consume("states", len(base.graphs) + len(added) + len(removed))
    existing, additions, removals = set(base.graphs), set(added), set(removed)
    if additions & existing or removals - existing or additions & removals:
        raise Rejected(
            "rdf.graph_inventory_conflict",
            "Graph inventory edits require new additions and existing removals",
        )
    graphs = (existing | additions) - removals
    if base.kind != "dataset" and (added or removed):
        raise PatchUnavailable("A pinned graph cannot change its authorized inventory")
    by_graph: dict[PatchResource | None, list[_Route]] = defaultdict(list)
    for route in routes:
        budget.consume("work")
        source, target = (
            _term(route.source, captured, budget),
            _term(route.target, captured, budget),
        )
        if source is not None and source not in existing:
            raise Rejected("rdf.graph_route_scope", "A route source graph is absent")
        if target is not None and target not in graphs:
            raise Rejected(
                "rdf.graph_route_scope",
                "A route target requires a retained or explicitly added graph",
            )
        if base.kind != "dataset" and source != target:
            raise PatchUnavailable("Graph routing cannot widen a pinned selection")
        if base.kind == "named" and source != base.graph_selection:
            raise Rejected(
                "rdf.graph_route_scope", "A route lies outside the pinned graph"
            )
        subjects = None
        predicates = None
        if route.subjects is not None:
            budget.consume("states", len(route.subjects))
            subjects = frozenset(
                cast(PatchResource, _term(value, captured, budget))
                for value in route.subjects
            )
        if route.predicates is not None:
            budget.consume("states", len(route.predicates))
            for value in route.predicates:
                _resource(value, budget)
            predicates = frozenset(URIRef(value) for value in route.predicates)
        budget.consume("states")
        by_graph[source].append(_Route(target, subjects, predicates))
    result = []
    unique: set[PatchQuad] = set()
    contributors: dict[PatchResource | None, set[PatchResource | None]] = defaultdict(
        set
    )
    for quad in base.quads:
        budget.consume("work")
        destinations: set[PatchResource | None] = set()
        for matching in by_graph.get(quad.graph, ()):
            budget.consume("work")
            if (
                matching.subjects is not None
                and quad.subject not in matching.subjects
                or matching.predicates is not None
                and quad.predicate not in matching.predicates
            ):
                continue
            budget.consume("states")
            destinations.add(matching.target)
        if len(destinations) > 1:
            raise Rejected(
                "rdf.graph_route_collision",
                "Overlapping routes assign an original quad to different graphs",
            )
        destination = next(iter(destinations)) if destinations else quad.graph
        if destination is not None and destination not in graphs:
            raise Rejected(
                "rdf.graph_inventory_conflict",
                "A removed graph still contains unselected assertions",
            )
        owners = contributors.get(destination)
        if owners is None:
            budget.consume("states")
            owners = set()
            contributors[destination] = owners
        if quad.graph not in owners:
            budget.consume("states")
            owners.add(quad.graph)
        if not merge and len(owners) > 1:
            raise Rejected(
                "rdf.graph_route_collision",
                "Distinct graph partitions require explicit merge=True",
            )
        budget.consume("states")
        changed = PatchQuad(quad.subject, quad.predicate, quad.value, destination)
        if changed not in unique:
            budget.consume("states")
            unique.add(changed)
            result.append(changed)
    budget.consume("states", len(base.quads) + len(result))
    budget.consume("work", len(base.quads) + len(result))
    before = set(base.quads)
    target_witness = SnapshotWitness(
        base.kind, base.graph_selection, tuple(result), tuple(graphs)
    )
    return DatasetPatch(
        base,
        target_witness,
        tuple(quad for quad in base.quads if quad not in unique),
        tuple(quad for quad in result if quad not in before),
        removed,
        added,
    )


def migrate_graphs(
    source: PreparedSource,
    routes: Sequence[GraphRoute],
    *,
    merge: bool = False,
    added_graphs: Sequence[URIRef | BNode] = (),
    removed_graphs: Sequence[URIRef | BNode] = (),
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> MigrationCandidate:
    """Move explicit selected quads, then retain distinct application contracts.

    All rules match original graph membership; routing is simultaneous. Merging
    competing original partitions requires merge=True. Named graph creation and
    deletion require the separate explicit inventories. Untouched graphs and
    shared blank roles are preserved; union input cannot supply this contract.
    """
    if type(merge) is not bool:
        raise InputError("Graph merge policy requires an explicit boolean")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    patch = None
    transformed = None
    try:
        selected = capture_items(routes, GraphRoute, operation)
        captured = capture_source(source, "base:", operation)
        added = _inventory(added_graphs, captured, operation)
        removed = _inventory(removed_graphs, captured, operation)
        if not selected and not added and not removed:
            raise InputError(
                "Graph migration requires explicit routes or inventory edits"
            )
        patch = _patch(captured, selected, added, removed, merge, operation)
        operation.consume("states", len(captured.owned))
        bindings: dict[str, PatchBlankBinding] = {}
        for node, ref in captured.owned.values():
            operation.consume("work")
            bindings.setdefault(ref.token, PatchBlankBinding(ref.token, node))
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("Graph migration exceeded its wall-clock deadline")
        transformed = apply_rdf_patch(
            source,
            patch,
            blank_bindings=tuple(bindings.values()),
            budget=operation,
            timeout=remaining,
            cancel=cancel,
        )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.graph_migration")
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(
                Issue("rdf.graph_scope_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="rdf.graph_migration")
    except Rejected as error:
        try:
            issues.append(Issue(error.code, str(error), kind="input_error"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="rdf.graph_migration")
    if transformed is None:
        transformed = Transformation(
            None, report("rdf.graph_routing", issues, complete=False)
        )
    return finish_migration(
        source,
        transformed,
        patch,
        before_checks=before_checks,
        after_checks=after_checks,
        budget=operation,
    )
