"""Bounded sequential patch execution with current-base and final-state checks."""

import threading
import time
from collections.abc import Sequence

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate, MigrationCheck
from bijux_ontology.rdf.patches.applying import _materialize, apply_rdf_patch
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.rdf.snapshots import PreparedSource

from .types import MigrationPlanResult, MigrationStep, MigrationStepResult


def _remaining(operation: Operation) -> float:
    """Keep nested owned workers inside the plan's existing absolute deadline."""
    operation.consume("work", 0)
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Migration plan exceeded its wall-clock deadline")
    return remaining


def _checks(
    source: PreparedSource,
    declarations: Sequence[MigrationCheck],
    scope: str,
    operation: Operation,
    issues: IssueCollector,
) -> Report:
    """Adopt each actual report once, retaining a failed prefix on exhaustion."""
    start = len(issues)
    reports: list[Report] = []
    requested = 1
    complete = True
    try:
        selected = capture_items(declarations, MigrationCheck, operation)
        requested = max(1, len(selected))
        seen: set[str] = set()
        if not selected:
            issues.append(
                Issue(
                    "upgrade.plan_unverified",
                    "Required migration conditions were not supplied",
                    scope,
                    kind="unverified",
                )
            )
        captured = capture_source(source, "condition:", operation)
        for declaration in selected:
            bound_text(declaration.name, operation)
            if declaration.name in seen:
                raise InputError("Migration condition names must be distinct per gate")
            operation.consume("states", 2)
            operation.consume("bytes", 64)
            seen.add(declaration.name)
            report = declaration.check(
                _materialize(captured.witness, source, operation)
            )
            if not isinstance(report, Report):
                raise InputError("Migration conditions must return Report")
            retain_report(report, operation, issues)
            if not report.checked:
                issues.append(
                    Issue(
                        "upgrade.plan_unverified",
                        "A required condition performed no actual comparison",
                        declaration.name,
                        kind="unverified",
                    )
                )
            reports.append(Report(declaration.name, 1, children=(report,)))
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error), location=scope)
    return Report(
        scope,
        len(reports),
        tuple(issues[start:]),
        requested=requested,
        children=tuple(reports),
        complete=complete,
    )


def execute_migration_plan(
    source: PreparedSource,
    steps: Sequence[MigrationStep],
    *,
    final_checks: Sequence[MigrationCheck],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> MigrationPlanResult:
    """Execute a dry run without changing any caller graph or file.

    Preconditions, native candidate truth, current-base patch application, exact
    candidate comparison and postconditions must all verify before advancing.
    Final checks run again on the actual final source. Failed or exhausted gates
    expose their named operation and withhold the final candidate. Symmetric
    affected blank identities that the native patch cannot resolve stay unknown.
    Callback programmer exceptions propagate; no in-process preemption is claimed.
    """
    if not isinstance(source, PreparedSource):
        raise InputError("Migration plans require an owned PreparedSource")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    results: list[MigrationStepResult] = []
    children: list[Report] = []
    completed = 0
    requested = 1
    active = "migration.plan"
    current = None
    final = None
    witness = None
    try:
        selected = capture_items(steps, MigrationStep, operation)
        final_declarations = capture_items(final_checks, MigrationCheck, operation)
        requested = len(selected) + 1
        names: set[str] = set()
        for step in selected:
            bound_text(step.name, operation)
            if step.name in names:
                raise InputError("Migration operation names must be distinct")
            operation.consume("states")
            names.add(step.name)
        if not selected:
            issues.append(
                Issue(
                    "upgrade.plan_unverified",
                    "A required migration plan contains no operations",
                    kind="unverified",
                )
            )
        captured = capture_source(source, "plan:", operation)
        current = _materialize(captured.witness, source, operation)
        for step in selected:
            active = step.name
            gates = []
            candidate = None
            pre = _checks(
                current,
                step.preconditions,
                "migration.preconditions",
                operation,
                issues,
            )
            gates.append(pre)
            if pre.verified:
                actual = capture_source(current, "operation:", operation)
                callback_source = _materialize(actual.witness, current, operation)
                candidate = step.transform(callback_source, operation)
                if not isinstance(candidate, MigrationCandidate):
                    raise InputError(
                        "Migration operations must return MigrationCandidate"
                    )
                retain_report(candidate.report, operation, issues)
                gates.append(candidate.report)
                if candidate.report.verified:
                    if candidate.source is None or candidate.patch is None:
                        missing = Issue(
                            "upgrade.plan_unverified",
                            "Migration returned no exact patch or candidate",
                            kind="unverified",
                        )
                        issues.append(missing)
                        gates.append(
                            Report(
                                "migration.candidate", issues=(missing,), requested=1
                            )
                        )
                    else:
                        applied = apply_rdf_patch(
                            current,
                            candidate.patch,
                            budget=operation,
                            timeout=_remaining(operation),
                            cancel=cancel,
                        )
                        retain_report(applied.report, operation, issues)
                        gates.append(applied.report)
                        if applied.source is not None and applied.report.verified:
                            compared = compare_rdf(
                                applied.source,
                                candidate.source,
                                budget=operation,
                                timeout_seconds=_remaining(operation),
                            )
                            retain_report(compared, operation, issues)
                            gates.append(compared)
                            if compared.verified:
                                post = _checks(
                                    applied.source,
                                    step.postconditions,
                                    "migration.postconditions",
                                    operation,
                                    issues,
                                )
                                gates.append(post)
                                if post.verified:
                                    current = applied.source
            report = Report(
                step.name,
                len(gates),
                requested=max(1, len(gates)),
                children=tuple(gates),
            )
            operation.consume("states")
            results.append(MigrationStepResult(step.name, candidate, report))
            children.append(report)
            if not report.verified:
                break
            completed += 1
        if selected and completed == len(selected):
            active = "migration.final"
            checked = _checks(current, final_declarations, active, operation, issues)
            children.append(checked)
            if checked.verified:
                completed += 1
                witness = capture_source(current, "final:", operation).witness
                final = current
    except PatchUnavailable as error:
        try:
            issues.append(
                Issue("upgrade.plan_unverified", str(error), active, kind="unverified")
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location=active)
    except LimitExceeded as error:
        final = None
        issues.exhausted(str(error), location=active)
    report = Report(
        "migration.plan",
        len(children),
        tuple(issues),
        requested=requested,
        children=tuple(children),
        regime="checked-sequential-rdf-migration",
    )
    return MigrationPlanResult._from_execution(
        final if report.verified else None,
        tuple(results),
        report,
        witness if report.verified else None,
    )
