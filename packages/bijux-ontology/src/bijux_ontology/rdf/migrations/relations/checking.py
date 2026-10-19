"""Atomic relation representation candidates with actual selected output checks."""

import threading
import time
from collections.abc import Sequence
from dataclasses import replace

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCandidate,
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import capture_items, capture_source
from bijux_ontology.rdf.patches.types import PatchBlankBinding, PatchUnavailable
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report

from .planning import RelationRejected, Request, plan_relations, reserve
from .types import BinaryOccurrence, RelationOccurrence, RelationPattern


def migrate_relations(
    source: PreparedSource,
    pattern: RelationPattern,
    occurrences: Sequence[BinaryOccurrence | RelationOccurrence],
    *,
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> MigrationCandidate:
    """Convert explicitly selected positive relation representations transactionally.

    BinaryOccurrence expands an exact edge into a caller-allocated event IRI,
    declared class/roles and supplied qualifiers. RelationOccurrence collapses an
    isolated unqualified event with exactly one participant per role. Extra
    types, qualifiers, incoming uses and competing output edges prevent lossless
    collapse. Negative patterns/OWL negative assertions never create positives.

    The patch keeps exact base, term, blank-sharing and physical graph inventory
    preconditions. All requests address original facts, never an intermediate
    candidate. Caller after-checks must actually qualify the copied output before
    ordinary explicit migration acceptance/export can succeed. Supplied roles
    declare application meaning; no OWL equivalence or global freshness is proved.
    """
    if type(pattern) is not RelationPattern:
        raise InputError("Relation migrations require a typed caller-authored pattern")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    patch = None
    applied = None
    count = 0
    requested = 1
    try:
        metadata = SnapshotMetadata.capture(source, operation)
        captured = capture_source(source, "base:", operation)
        supplied = capture_items(occurrences, object, operation)
        requested = max(1, len(supplied))
        reserve(operation, len(supplied))
        requests: list[Request] = []
        for request in supplied:
            operation.consume("work")
            if type(request) not in (BinaryOccurrence, RelationOccurrence):
                raise InputError("Use typed original binary or relation occurrences")
            assert isinstance(request, BinaryOccurrence | RelationOccurrence)
            requests.append(request)
        patch = plan_relations(captured, pattern, tuple(requests), operation)
        reserve(operation, len(captured.owned))
        bindings: dict[str, PatchBlankBinding] = {}
        for node, reference in captured.owned.values():
            operation.consume("work")
            bindings.setdefault(
                reference.token, PatchBlankBinding(reference.token, node)
            )
        if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
            raise PatchUnavailable("Original relation source scope metadata changed")
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("Relation migration exceeded its deadline")
        applied = apply_rdf_patch(
            source,
            patch,
            blank_bindings=tuple(bindings.values()),
            budget=operation,
            timeout=remaining,
            cancel=cancel,
        )
        retain_report(applied.report, operation, issues)
        children.append(applied.report)
        if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
            raise PatchUnavailable("Original relation source scope metadata changed")
        if applied.report.verified:
            count = len(requests)
    except LimitExceeded as error:
        if applied is not None:
            issues.retain_completed(applied.report)
        issues.exhausted(str(error))
    except RelationRejected as error:
        count = error.index + 1
        try:
            issues.append(Issue(error.code, str(error), f"occurrence[{error.index}]"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(
                Issue(
                    "rdf.relation_representation_unverified",
                    str(error),
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    except InputError as error:
        try:
            issues.append(
                Issue("rdf.relation_input_invalid", str(error), kind="input_error")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    raw = Report(
        "rdf.relation_representation",
        count,
        tuple(issues),
        requested=requested,
        complete=count > 0 and applied is not None and applied.report.verified,
        children=tuple(children),
        regime="caller-declared-physical-relation-occurrences",
    )
    available = applied.source if applied is not None and raw.verified else None
    finished = finish_migration(
        source,
        Transformation(available, raw),
        patch,
        before_checks=before_checks,
        after_checks=after_checks,
        budget=operation,
    )
    if not finished.report.verified:
        # Final contract adoption may itself hit the same cumulative cap. Keep
        # the transform's already-paid prefix even before its child is attached.
        terminal = IssueCollector(operation)
        terminal.retain_completed(raw)
        terminal.retain_completed(finished.report)
        for receipt in (raw, finished.report):
            for issue in receipt.issues:
                if issue.kind == "exhausted":
                    terminal.exhausted(issue.message, location=issue.location)
                    break
        finished = replace(
            finished, report=replace(finished.report, issues=tuple(terminal))
        )
    return finished
