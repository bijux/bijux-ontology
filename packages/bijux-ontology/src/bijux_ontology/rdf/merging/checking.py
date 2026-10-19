"""Resolve an owned plan, apply its exact patch and run ordinary selected checks."""

import threading
import time
from collections.abc import Sequence

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.planning import edited
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlankBinding,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    bound_term,
    validate_witness,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report

from .types import MergeChanges, MergeResolution, RdfMergePlan, RdfMergeResult


def _collect(
    change: MergeChanges,
    records: tuple[
        set[PatchQuad], set[PatchQuad], set[PatchResource], set[PatchResource]
    ],
    operation: Budget,
) -> None:
    """Adopt exact edits once after bounding term metadata before set hashing."""
    for quads, quad_set in (
        (change.removals, records[0]),
        (change.additions, records[1]),
    ):
        for quad in quads:
            operation.consume("work")
            for term in (quad.subject, quad.predicate, quad.value, quad.graph):
                if term is not None:
                    bound_term(term, operation)
            operation.consume("states")
            operation.consume("bytes", 16)
            quad_set.add(quad)
    for graphs, graph_set in (
        (change.removed_graphs, records[2]),
        (change.added_graphs, records[3]),
    ):
        for graph in graphs:
            operation.consume("work")
            bound_term(graph, operation)
            operation.consume("states")
            operation.consume("bytes", 16)
            graph_set.add(graph)


def apply_rdf_merge(
    base: PreparedSource,
    plan: RdfMergePlan,
    *,
    resolutions: Sequence[MergeResolution] = (),
    blank_bindings: Sequence[PatchBlankBinding] = (),
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> RdfMergeResult:
    """Require every explicit decision and exact current base before publishing.

    Raw conflict findings remain in result.plan.report. Resolved structural truth
    is separately reported; it never rewrites that history. Ordinary selected
    after contracts determine application acceptance, and their absence remains
    required unknown even when a structurally checked candidate is available.
    A later independently parsed base uses maintained unique correspondence or
    explicit actual owned bindings; parser labels never transfer identity.
    """
    if not isinstance(plan, RdfMergePlan) or not isinstance(base, PreparedSource):
        raise InputError("RDF merge application requires an owned plan and source")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    history = IssueCollector(operation)
    supplied: tuple[MergeResolution, ...] = ()
    children: list[Report] = []
    patch = None
    applied = None
    checked = 0
    try:
        # History is owned and budgeted, but is not relabeled as successful truth.
        retain_report(plan.report, operation, history)
        if not plan._ready or plan.base is None:
            issues.extend(history)
            issues.append(
                Issue(
                    "rdf.merge_unverified",
                    "The common-base merge plan did not complete",
                    kind="unverified",
                )
            )
        else:
            validate_witness(plan.base, operation)
            supplied = capture_items(resolutions, MergeResolution, operation)
            explicit = capture_items(blank_bindings, PatchBlankBinding, operation)
            operation.consume("states", len(supplied))
            selected: dict[int, str] = {}
            for decision in supplied:
                operation.consume("work")
                if (
                    decision.conflict >= len(plan.conflicts)
                    or decision.conflict in selected
                ):
                    raise InputError(
                        "Merge resolutions require distinct existing "
                        "conflict identifiers"
                    )
                selected[decision.conflict] = decision.choice
            if len(selected) != len(plan.conflicts):
                issues.append(
                    Issue(
                        "rdf.merge_resolution_required",
                        "Every structural conflict requires an explicit "
                        "caller decision",
                        kind="unverified",
                    )
                )
            else:
                records: tuple[
                    set[PatchQuad],
                    set[PatchQuad],
                    set[PatchResource],
                    set[PatchResource],
                ] = (set(), set(), set(), set())
                for component in plan._components:
                    operation.consume("work")
                    if component.conflict is None:
                        _collect(component.left, records, operation)
                        _collect(component.right, records, operation)
                    else:
                        choice = selected[component.conflict]
                        if choice != "base":
                            _collect(
                                component.left if choice == "left" else component.right,
                                records,
                                operation,
                            )
                removals, additions = tuple(records[0]), tuple(records[1])
                removed, added = tuple(records[2]), tuple(records[3])
                target = edited(
                    plan.base, removals, additions, removed, added, operation
                )
                patch = DatasetPatch(
                    plan.base, target, removals, additions, removed, added
                )
                remaining = operation.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("RDF merge exceeded its wall-clock deadline")
                bindings = (
                    explicit if explicit or base is not plan._owner else plan._bindings
                )
                applied = apply_rdf_patch(
                    base,
                    patch,
                    blank_bindings=bindings,
                    budget=operation,
                    timeout=remaining,
                    cancel=cancel,
                )
                retain_report(applied.report, operation, issues)
                children.append(applied.report)
                checked = applied.report.checked
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.merge")
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(Issue("rdf.merge_unverified", str(error), kind="unverified"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="rdf.merge")
    raw = Report(
        "rdf.merge_structural",
        checked,
        tuple(issues),
        requested=1,
        complete=applied is not None and applied.report.verified,
        children=tuple(children),
        regime="exact-common-base-structural-merge",
    )
    candidate = None
    if applied is not None and applied.source is not None and raw.verified:
        candidate = finish_migration(
            base,
            Transformation(applied.source, raw),
            patch,
            before_checks=before_checks,
            after_checks=after_checks,
            budget=operation,
        )
    return RdfMergeResult(
        plan,
        supplied,
        candidate,
        raw,
        candidate.report if candidate is not None else raw,
    )
