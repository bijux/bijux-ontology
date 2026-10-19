"""Atomic revision publication after exact asserted deltas and fresh closure."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.types import DatasetPatch
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Issue, Report

from .execution import execute, own_plan, own_source
from .types import (
    MaterializationPlan,
    MaterializationRevision,
    MaterializationUpdate,
)


@dataclass(frozen=True)
class _State:
    """Private asserted dependency input, owned program and matching revision."""

    asserted: PreparedSource
    plan: MaterializationPlan
    revision: MaterializationRevision


class MaterializationSession:
    """One bounded accepted asserted revision with maintained complete recomputation.

    Native backends expose no qualified incremental support index. Retaining the
    entire asserted dependency input and program permits correct full recomputation
    for deletions, changed rules and nonmonotone supported programs. A failed update
    accepts neither new assertions nor a new closure. Returned native results retain
    their own joint blank identity; their public metadata cannot alter private state.
    """

    _guard: threading.Lock
    _state: _State | None

    def __init__(self) -> None:
        """Require preparation instead of an empty apparently accepted session."""
        raise InputError("Use prepare_materialization to create an accepted session")

    @classmethod
    def _owned(cls, state: _State) -> "MaterializationSession":
        """Adopt only successfully materialized private asserted/program inputs."""
        result = object.__new__(cls)
        result._guard = threading.Lock()
        result._state = state
        return result

    @contextmanager
    def _locked(self, operation: Operation) -> Iterator[None]:
        """Bound lock acquisition by the same overall deadline and cancellation."""
        while True:
            operation.consume("work", 0)
            if self._guard.acquire(
                timeout=min(0.05, max(0, operation.deadline - time.monotonic()))
            ):
                break
        try:
            operation.consume("work", 0)
            yield
        finally:
            self._guard.release()

    @property
    def current_revision(self) -> MaterializationRevision | None:
        """Return the last accepted receipt; no new asserted data is implied."""
        state = self._state
        return state.revision if state is not None else None

    def close(self) -> None:
        """Release private dependency inputs and refuse concurrent late publication."""
        with self._guard:
            self._state = None

    def update(
        self,
        patch: DatasetPatch | None,
        *,
        expected: MaterializationRevision,
        plan: MaterializationPlan | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: threading.Event | None = None,
    ) -> MaterializationUpdate:
        """Apply a checked asserted patch and atomically publish a complete revision.

        Replacing a program may omit a data patch. Both changes still recompute
        the whole selected closure. Concurrent updates compute independently; only
        the update whose expected receipt is still current can publish. Callbacks
        run outside the state lock and retain their ordinary in-process semantics.
        Anonymous patches require uniquely established structural correspondence;
        ambiguous private blank targets remain unavailable.
        """
        operation = Operation(limits, budget, timeout, cancel)
        issues = IssueCollector(operation)
        children: list[Report] = []
        previous = None
        try:
            with self._locked(operation):
                state = self._state
                if state is None or state.revision is not expected:
                    raise InputError(
                        "Expected revision is no longer the active session state"
                    )
                previous = state.revision
            if patch is None and plan is None:
                raise InputError(
                    "An update requires an asserted patch or replacement program"
                )
            candidate = state.asserted
            if patch is not None:
                applied = apply_rdf_patch(
                    state.asserted,
                    patch,
                    budget=operation,
                    timeout=max(0.000001, operation.deadline - time.monotonic()),
                    cancel=cancel,
                )
                retain_report(applied.report, operation, issues)
                children.append(applied.report)
                if applied.source is None or not applied.report.verified:
                    return _failure(previous, issues, children)
                candidate = applied.source
            owned = own_source(candidate, operation)
            selected = own_plan(plan, operation) if plan is not None else state.plan
            native = execute(owned, selected, operation)
            retain_report(native.report, operation, issues)
            children.append(native.report)
            if not native.report.verified:
                return _failure(previous, issues, children)
            operation.consume("states", 3)
            operation.consume("bytes", 192)
            revision = MaterializationRevision._owned(state.revision.number + 1)
            next_state = _State(owned, selected, revision)
            report = Report(
                "rdf.materialization.update",
                1,
                tuple(issues),
                requested=1,
                children=tuple(children),
                regime="complete-recomputation",
            )
            result = MaterializationUpdate(revision, previous, native, report)
            with self._locked(operation):
                if self._state is not state:
                    raise InputError(
                        "Another update or close superseded the expected revision"
                    )
                operation.consume("work", 0)
                self._state = next_state
            return result
        except LimitExceeded as error:
            issues.exhausted(str(error), location="rdf.materialization")
        except InputError:
            _invalid(issues)
        return _failure(previous, issues, children)


@dataclass(frozen=True)
class MaterializationPreparation:
    """A session is available only when its first actual closure was accepted."""

    session: "MaterializationSession | None"
    result: MaterializationUpdate


def _invalid(issues: IssueCollector) -> None:
    """Retain one bounded input or revision conflict diagnostic."""
    try:
        issues.append(
            Issue(
                "rdf.materialization_input_error",
                "Materialization scope, update or expected revision is invalid",
                kind="input_error",
            )
        )
    except LimitExceeded:
        issues.exhausted("Stopped retaining materialization input failure")


def _failure(
    previous: MaterializationRevision | None,
    issues: IssueCollector,
    children: list[Report],
) -> MaterializationUpdate:
    """Never label the preceding accepted closure as the failed update's output."""
    return MaterializationUpdate(
        None,
        previous,
        None,
        Report(
            "rdf.materialization.update",
            int(any(child.checked for child in children)),
            tuple(issues),
            requested=1,
            complete=False,
            children=tuple(children),
            regime="complete-recomputation",
        ),
    )


def prepare_materialization(
    source: PreparedSource,
    plan: MaterializationPlan,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> MaterializationPreparation:
    """Own exact asserted input and program, then accept the first actual closure."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    try:
        owned = own_source(source, operation)
        selected = own_plan(plan, operation)
        native = execute(owned, selected, operation)
        retain_report(native.report, operation, issues)
        children.append(native.report)
        if native.report.verified:
            operation.consume("states", 4)
            operation.consume("bytes", 256)
            revision = MaterializationRevision._owned(1)
            session = MaterializationSession._owned(_State(owned, selected, revision))
            result = MaterializationUpdate(
                revision,
                None,
                native,
                Report(
                    "rdf.materialization.prepare",
                    1,
                    tuple(issues),
                    requested=1,
                    children=tuple(children),
                    regime="complete-recomputation",
                ),
            )
            operation.consume("work", 0)
            return MaterializationPreparation(session, result)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.materialization")
    except InputError:
        _invalid(issues)
    return MaterializationPreparation(None, _failure(None, issues, children))
