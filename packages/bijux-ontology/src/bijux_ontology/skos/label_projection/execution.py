"""Physical label snapshot preparation and checked patch/candidate handoff."""

import time
from collections.abc import Sequence
from dataclasses import replace

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import IssueCollector, retain_report
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlankBinding,
    PatchQuad,
    PatchUnavailable,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.skos.checking import check_skos

from .types import LabelCandidate, LabelCorrespondence


def capture(source: PreparedSource, operation: Operation) -> Captured:
    """Require one authorized physical graph, never a dataset union projection."""
    captured = capture_source(source, "labels:", operation)
    if captured.witness.kind == "dataset":
        raise PatchUnavailable(
            "Label transformations require one selected physical graph"
        )
    return captured


def integrity(
    source: PreparedSource, operation: Operation, issues: IssueCollector
) -> Report:
    """Use ordinary SKOS integrity, lexical and XL checks on the actual graph."""
    checked = check_skos(
        source._read(budget=operation),
        rules=("label-integrity", "resource-integrity", "lexical-labels", "xl"),
        budget=operation,
    )
    retain_report(checked, operation, issues)
    return checked


def unavailable(issues: IssueCollector, error: PatchUnavailable) -> None:
    """Preserve a bounded required-unknown diagnostic even near exhaustion."""
    try:
        issues.append(Issue("skos.labels_unverified", str(error), kind="unverified"))
    except LimitExceeded as exhausted:
        issues.exhausted(str(exhausted))


def finish(
    source: PreparedSource,
    captured: Captured | None,
    output: set[PatchQuad] | None,
    correspondence: list[LabelCorrespondence],
    omitted: list[PatchQuad],
    issues: IssueCollector,
    children: list[Report],
    operation: Operation,
    after_checks: Sequence[MigrationCheck],
) -> LabelCandidate:
    """Apply existing exact patch semantics and require real consumer contracts."""
    patch = None
    transformed = None
    checks: tuple[MigrationCheck, ...] = ()
    try:
        checks = capture_items(after_checks, MigrationCheck, operation)
        if not checks:
            issues.append(
                Issue(
                    "skos.label_consumer_unverified",
                    "Label transformation requires an explicit consumer after check",
                    kind="unverified",
                )
            )
        if output is not None and captured is not None and not issues:
            operation.consume("states", len(captured.witness.quads) + len(output))
            operation.consume("work", len(captured.witness.quads) + len(output))
            before = set(captured.witness.quads)
            target = replace(captured.witness, quads=tuple(output))
            patch = DatasetPatch(
                captured.witness,
                target,
                removals=tuple(before - output),
                additions=tuple(output - before),
            )
            bindings = []
            used = set()
            for node, reference in captured.owned.values():
                operation.consume("work")
                if reference.token not in used:
                    operation.consume("states", 2)
                    used.add(reference.token)
                    bindings.append(PatchBlankBinding(reference.token, node))
            operation.consume("states", 2)
            transformed = apply_rdf_patch(
                source,
                patch,
                blank_bindings=tuple(bindings),
                budget=operation,
                timeout=max(0.001, operation.deadline - time.monotonic()),
                cancel=operation.cancel,
            )
            retain_report(transformed.report, operation, issues)
            children.append(transformed.report)
            if transformed.source is not None and transformed.report.verified:
                children.append(integrity(transformed.source, operation, issues))
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except PatchUnavailable as error:
        unavailable(issues, error)
    raw = Report(
        "skos.label_transformation",
        len(correspondence),
        tuple(issues),
        requested=max(1, len(correspondence)),
        children=tuple(children),
        regime="selected-physical-label-representation",
    )
    candidate = finish_migration(
        source,
        Transformation(
            transformed.source if transformed is not None and raw.verified else None,
            raw,
        ),
        patch,
        before_checks=(),
        after_checks=checks,
        budget=operation,
    )
    return LabelCandidate(
        candidate.source,
        candidate.patch,
        candidate.before,
        candidate.after,
        candidate.report,
        tuple(correspondence),
        tuple(omitted),
        _metadata=candidate._metadata,
        _checked_source=candidate._checked_source,
    )
