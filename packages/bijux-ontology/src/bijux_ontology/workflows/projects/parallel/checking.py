"""Dependency-aware isolated project checks with deterministic required outcomes."""

import json
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, BudgetKind, IssueCollector, retain_report
from bijux_ontology.rdf.execution import checkpoint, execute_worker
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report, combine_reports, report_from_data
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.jobs import dependencies

from .preparation import KINDS, _metadata_bound, capture, supported
from .types import ParallelJobResult, ParallelProjectResult


def _pending(name: str, reason: str) -> ParallelJobResult:
    """Make an unstarted required scope visible without inventing a comparison."""
    return ParallelJobResult(
        name,
        "pending",
        Report(
            name,
            issues=(
                Issue(
                    "project.job_pending",
                    "Required job did not start",
                    kind="unverified",
                ),
            ),
            requested=1,
            complete=False,
        ),
        reason=reason,
    )


def _reply(raw: bytes, name: str, grant: dict[BudgetKind, int]) -> ParallelJobResult:
    """Validate typed native reports and exact finite receipts before adoption."""
    value = decode_json(raw, max_bytes=len(raw))
    if not isinstance(value, dict) or not isinstance(value.get("consumed"), dict):
        raise InputError("Invalid parallel job receipt")
    consumed = value["consumed"]
    if set(consumed) != set(KINDS) or any(
        type(consumed[kind]) is not int or not 0 <= consumed[kind] <= grant[kind]
        for kind in KINDS
    ):
        raise InputError("Invalid parallel job consumption")
    started, finished = value.get("started_ns"), value.get("finished_ns")
    if (
        type(started) is not int
        or type(finished) is not int
        or not 0 < started <= finished
    ):
        raise InputError("Invalid parallel job timing")
    return ParallelJobResult(
        name,
        "completed",
        replace(report_from_data(value.get("report")), scope=name),
        tuple((kind, consumed[kind]) for kind in KINDS),
        tuple(grant.items()),
        started,
        finished,
    )


def _execute(
    manifest: Path,
    index: int,
    name: str,
    grant: dict[BudgetKind, int],
    limits: object,
    cap: int,
    timeout: float,
    cancel: threading.Event,
) -> ParallelJobResult:
    """Supervise only process IO; each worker owns its own graphs and caches."""
    payload = json.dumps(
        {
            "manifest": str(manifest),
            "index": index,
            "limits": limits,
            "allowances": grant,
        }
    ).encode()
    received: ParallelJobResult | None = None

    def completed_reply(raw: bytes) -> None:
        """Qualify one complete native receipt before a later stop can reject IO."""
        nonlocal received
        received = _reply(raw, name, grant)

    try:
        execute_worker(
            "bijux_ontology.workflows.projects.parallel.worker",
            payload,
            cap=cap,
            timeout=timeout,
            cancel=cancel,
            completed=completed_reply,
        )
        if received is None:
            raise InputError("Parallel worker omitted its completed receipt")
        return received
    except (InputError, ResourceError, OSError) as error:
        if received is not None and isinstance(error, LimitExceeded):
            if cancel.is_set():
                return received
            report = combine_reports(
                name,
                received.report,
                Report(
                    name,
                    issues=(
                        Issue(
                            "execution.limit_exceeded",
                            "Project job reached its deadline during receipt handoff",
                            kind="exhausted",
                        ),
                    ),
                    requested=1,
                    complete=False,
                ),
            )
            return replace(
                received, state="interrupted", report=report, reason="worker-deadline"
            )
        # Missing receipts cannot establish unspent work. Forfeit the full grant
        # and retain the interrupted required scope; never refund guessed work.
        report = Report(
            name,
            0,
            (
                Issue(
                    "execution.limit_exceeded",
                    "Project worker did not complete its bounded execution",
                    kind="exhausted",
                ),
            ),
            requested=1,
            complete=False,
        )
        return ParallelJobResult(
            name,
            "interrupted",
            report,
            tuple(grant.items()),
            tuple(grant.items()),
            reason="worker-incomplete",
        )


def check_project_parallel(
    configuration: ProjectConfig,
    *,
    workers: int = 2,
    timeout: float = 30.0,
    job_timeout: float = 30.0,
    budget: Budget | None = None,
    artifacts: Path,
    cancel: threading.Event | None = None,
    completed: Callable[[ParallelJobResult], None] | None = None,
) -> ParallelProjectResult:
    """Run finite vocabulary, context and SHACL jobs in separate owned processes.

    Only validated ProjectConfig inputs are supported; PreparedChecks revisions
    and runtime routed inputs must use their ordinary runner. Context corpus and
    other job kinds remain required unsupported scopes, never stale executions.
    Per-job disjoint grants and separate host transport reservations enforce the
    enclosing budget. Actual controlled counters are adopted once; terminated or
    malformed workers forfeit their entire grant. Native opaque engine CPU is
    bounded by process deadlines, not represented as instrumented work.

    Callbacks are delivered in declaration order after all required outcomes are
    settled. Dependencies use raw verification, independently of waiver policy.
    """
    if not isinstance(configuration, ProjectConfig):
        raise InputError("Parallel checks require an explicit ProjectConfig")
    if type(workers) is not int or not 1 <= workers <= 32:
        raise InputError("workers must be an integer between one and 32")
    if (
        type(job_timeout) not in (float, int)
        or not math.isfinite(job_timeout)
        or job_timeout <= 0
    ):
        raise InputError("job_timeout must be finite and positive")
    if not isinstance(artifacts, Path):
        raise InputError("Parallel checks require a caller-owned artifact directory")
    if completed is not None and not callable(completed):
        raise InputError("completed must be callable")
    operation = Operation(configuration.limits, budget, timeout, cancel)
    raw_jobs = configuration._data["checks"]
    assert isinstance(raw_jobs, list)
    count = len(raw_jobs)
    try:
        if count > operation.remaining("states") // 4:
            raise LimitExceeded("Project job inventory exceeds max_states")
        _metadata_bound(configuration._data, operation)
        operation.consume("states", count * 4)
        jobs = configuration.checks()
    except LimitExceeded:
        return ParallelProjectResult(
            (),
            Report(
                "project-parallel",
                0,
                (
                    Issue(
                        "execution.limit_exceeded",
                        "Project inventory could not be qualified",
                        kind="exhausted",
                    ),
                ),
                requested=count,
                complete=False,
            ),
        )
    names: list[str] = []
    results: dict[int, ParallelJobResult] = {}
    failure: str | None = None
    stop = threading.Event()
    try:
        for job in jobs:
            name = cast(str, job["name"])
            bound_text(name, operation)
            names.append(name)
        artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="project-checks-", dir=artifacts) as directory:
            manifest = capture(configuration, Path(directory), operation)
            count = len(jobs)
            # Reserve host output/decoding before granting semantic work. The
            # finite reply ceiling includes JSON framing and escaped metadata.
            unit = min(
                min(operation.remaining("bytes"), operation.remaining("work"))
                // (32 * count),
                max(0, operation.remaining("states") // (4 * count) - 512),
            )
            if unit < 1024:
                raise LimitExceeded("Insufficient parallel protocol allowance")
            cap = 4096 + 8 * unit
            operation.consume("bytes", 2 * cap * count)
            operation.consume("work", 3 * cap * count)
            operation.consume("states", 128 * count)
            issue_references = (cap // 4) * count
            operation.consume("states", issue_references)
            operation.consume("bytes", 64 * issue_references)
            operation.consume("work", issue_references)
            available = {kind: operation.remaining(kind) for kind in KINDS}
            grants: list[dict[BudgetKind, int]] = []
            for index in range(count):
                grant = {
                    kind: available[kind] // count
                    + int(index < available[kind] % count)
                    for kind in KINDS
                }
                grant["work"] = min(grant["work"], unit)
                grants.append(grant)
            # No parent budget mutations occur while grants are outstanding,
            # other than adopting those exact disjoint receipts below.
            running: dict[Future[ParallelJobResult], int] = {}
            name_indexes = {name: index for index, name in enumerate(names)}
            required_indexes = tuple(
                tuple(name_indexes[name] for name in dependencies(job)) for job in jobs
            )
            control_remaining = cap * count
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="project-check"
            ) as pool:
                while len(results) < count:
                    try:
                        checkpoint(operation.deadline, cancel)
                    except LimitExceeded:
                        stop.set()
                        failure = (
                            "Project execution was cancelled or reached "
                            "its global deadline"
                        )
                    progress = False
                    control_remaining -= count + sum(
                        len(items) for items in required_indexes
                    )
                    if control_remaining < 0:
                        stop.set()
                        failure = (
                            "Project scheduling exhausted its reserved "
                            "control allowance"
                        )
                    for index, job in enumerate(jobs):
                        if index in results or index in running.values():
                            continue
                        if failure is not None:
                            results[index] = _pending(names[index], "global-deadline")
                            continue
                        prior = required_indexes[index]
                        if any(item not in results for item in prior):
                            continue
                        if any(not results[item].report.verified for item in prior):
                            results[index] = _pending(
                                names[index], "dependency-unverified"
                            )
                            progress = True
                            continue
                        if not supported(job):
                            result = _pending(names[index], "unsupported-profile")
                            results[index] = ParallelJobResult(
                                result.name,
                                "unsupported",
                                result.report,
                                reason=result.reason,
                            )
                            progress = True
                            continue
                        if len(running) >= workers:
                            continue
                        remaining = operation.deadline - time.monotonic()
                        duration = job_timeout
                        declared = job.get("timeout")
                        if job.get("kind") == "shacl" and type(declared) in (
                            float,
                            int,
                        ):
                            numeric_timeout = cast(float, declared)
                            if math.isfinite(numeric_timeout) and numeric_timeout > 0:
                                duration = min(duration, numeric_timeout)
                        future = pool.submit(
                            _execute,
                            manifest,
                            index,
                            names[index],
                            grants[index],
                            asdict(configuration.limits),
                            cap,
                            min(duration, max(0.001, remaining)),
                            stop,
                        )
                        running[future] = index
                        progress = True
                    if not running:
                        if not progress and len(results) < count:
                            raise InputError(
                                "Project dependency scheduling made no progress"
                            )
                        continue
                    ready, _ = wait(running, timeout=0.01, return_when=FIRST_COMPLETED)
                    for future in ready:
                        index = running.pop(future)
                        result = future.result()
                        # Adoption still runs after cancellation: already bounded
                        # receipts must remain paid and completed reports survive.
                        for kind, amount in result.consumed:
                            operation.shared.consume(kind, amount)
                        try:
                            operation.shared._register_transferred_report(
                                result.report,
                                paid_issues=dict(result.consumed).get("issues", 0),
                                max_references=cap // 4,
                            )
                        except (InputError, LimitExceeded):
                            consumed = dict(result.consumed)
                            for kind, allowance in grants[index].items():
                                operation.shared.consume(
                                    kind, allowance - consumed[kind]
                                )
                            result = ParallelJobResult(
                                names[index],
                                "interrupted",
                                Report(
                                    names[index],
                                    issues=(
                                        Issue(
                                            "execution.limit_exceeded",
                                            "Project worker returned an invalid "
                                            "report receipt",
                                            kind="exhausted",
                                        ),
                                    ),
                                    requested=1,
                                    complete=False,
                                ),
                                tuple(grants[index].items()),
                                tuple(grants[index].items()),
                                reason="invalid-receipt",
                            )
                        results[index] = result
    except LimitExceeded:
        stop.set()
        failure = "Project execution exhausted its enclosing operation budget"
    finally:
        stop.set()
    try:
        checkpoint(operation.deadline, cancel)
    except LimitExceeded:
        failure = (
            failure or "Project execution was cancelled or reached its global deadline"
        )
    settled: list[ParallelJobResult] = []
    for index, _job in enumerate(jobs):
        result = results.get(index) or _pending(
            names[index] if index < len(names) else "unqualified-job",
            "preparation-incomplete",
        )
        if result.state in ("pending", "unsupported"):
            retained = IssueCollector(operation.shared)
            try:
                retain_report(result.report, operation.shared, retained)
            except LimitExceeded:
                retained.exhausted("Pending job qualification exhausted its budget")
                result = ParallelJobResult(
                    result.name,
                    result.state,
                    Report(
                        result.name, issues=tuple(retained), requested=1, complete=False
                    ),
                    reason=result.reason,
                )
        settled.append(result)
    ordered = tuple(settled)
    report = combine_reports("project-parallel", *(result.report for result in ordered))
    if failure is not None:
        report = combine_reports(
            "project-parallel",
            report,
            Report(
                "project-execution",
                0,
                (Issue("execution.limit_exceeded", failure, kind="exhausted"),),
                requested=1,
                complete=False,
            ),
        )
    if completed is not None:
        for result in ordered:
            completed(result)
    return ParallelProjectResult(ordered, report)
