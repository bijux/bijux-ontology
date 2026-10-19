"""Reuse explicit local resources while retaining every selected check outcome."""

import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import cast

from bijux_ontology._json import known_keys, object_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, IssueKind, Location, Report, combine_reports
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.inputs import ShaclJobInput, capture_job_inputs
from bijux_ontology.workflows.projects.jobs import (
    APPLICATION_KINDS,
    check_kind,
    dependencies,
)
from bijux_ontology.workflows.projects.operations import run_check
from bijux_ontology.workflows.projects.resources import ProjectResources


def _retain_report(
    reports: list[Report], report: Report, remaining: int
) -> tuple[int, bool]:
    if len(report.issues) > remaining:
        reports.append(
            replace(
                report,
                issues=report.issues[:remaining]
                + (
                    Issue(
                        "execution.limit_exceeded",
                        "Exceeded project max_issues",
                        kind="exhausted",
                    ),
                ),
                complete=False,
                children=(),
            )
        )
        return 0, False
    reports.append(report)
    return remaining - len(report.issues), True


def _replace_issue(report: Report, before: Issue, after: Issue) -> Report:
    """Keep aggregate and descendant evidence consistent when adding context."""
    return replace(
        report,
        issues=tuple(after if issue == before else issue for issue in report.issues),
        children=tuple(
            _replace_issue(child, before, after) for child in report.children
        ),
    )


def _pending_names(names: Iterable[str], count: int, budget: Budget) -> tuple[str, ...]:
    """Describe pending scopes only when their complete inventory fits the budget."""
    try:
        budget.consume("states", count)
        selected: list[str] = []
        size = 0
        for name in names:
            bound_text(name, budget)
            size += len(name.encode("utf-8")) + (2 if selected else 0)
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Project pending scope inventory exceeds max_bytes")
            selected.append(name)
        budget.consume("bytes", size)
        budget.consume("work", size)
        return tuple(selected)
    except LimitExceeded:
        return ()


def _aggregate(
    scope: str, reports: list[Report], pending_count: int, pending: tuple[str, ...]
) -> Report:
    result = combine_reports(scope, *reports)
    if not pending_count:
        return result
    for issue in reversed(result.issues):
        if issue.kind == "exhausted":
            described = replace(
                issue,
                message=issue.message
                + f"; {pending_count} required scopes were not started",
                expected=", ".join(pending) if pending else None,
            )
            result = _replace_issue(result, issue, described)
            break
    return replace(
        result,
        requested=(result.requested or 0) + pending_count,
        complete=False,
    )


def _dependency_report(
    job: dict[str, object], previous: Mapping[str, Report], budget: Budget
) -> Report | None:
    """Keep prerequisite truth separate from process-policy acceptance."""
    missing: list[str] = []
    size = 0
    for name in dependencies(job):
        bound_text(name, budget)
        report = previous.get(name)
        if report is None or not report.verified:
            budget.consume("states")
            size += len(name.encode("utf-8")) + (2 if missing else 0)
            missing.append(name)
    if not missing:
        return None
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Project dependency diagnostic exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", size)
    return Report(
        cast(str, job["name"]),
        requested=1,
        complete=False,
        issues=(
            Issue(
                "project.dependency_unverified",
                "Required earlier jobs did not verify; this job was not started",
                expected=", ".join(missing),
                kind="unverified",
            ),
        ),
    )


class PreparedChecks:
    """Own reused vocabulary and context resources for one explicit project.

    No global caches or implicit discovery are used. Cached inputs own first-use
    snapshots; explicit replacement returns a new revision with independent budgets.
    """

    def __init__(self, configuration: ProjectConfig) -> None:
        self.configuration = configuration
        self._resources = ProjectResources(configuration)

    def vocabulary(
        self,
        name: str,
        *,
        lookup: tuple[str, ...] = (),
        graph_name: str | None = None,
        union: bool = False,
    ) -> Vocabulary:
        """Reuse one selected vocabulary view owned by this project execution."""
        return self._resources.vocabulary(
            name, lookup=lookup, graph_name=graph_name, union=union
        )

    def source(self, name: str) -> PreparedSource:
        """Reuse one named source owned by this project execution."""
        return self._resources.source(name)

    def replace_resources(
        self,
        *,
        rdf: Mapping[str, RdfSource] | None = None,
        contexts: Mapping[str, object] | None = None,
    ) -> "PreparedChecks":
        """Return a new session with acquired replacements for registered inputs.

        RDF descriptors supply complete syntax, identity and graph selection.
        Context values are complete JSON documents or Paths. Replacement content
        is owned before return; later caller/path edits cannot alter the revision.
        Unchanged already acquired inputs and proven independent views are reused.
        A changed context conservatively rebuilds every source that used remote
        contexts and its dependent vocabularies. Models, shapes and results are
        checked afresh. Unread original paths retain first-use lazy semantics;
        this operation does not claim to have acquired the entire project.
        """
        revised = PreparedChecks(self.configuration)
        revised._resources = self._resources.replace_resources(
            rdf={} if rdf is None else rdf,
            contexts={} if contexts is None else contexts,
        )
        return revised

    def _context_batch(self, job: dict[str, object]) -> Report:
        members = job.get("documents")
        if not isinstance(members, list) or not members:
            raise InputError("Context batch documents must be a nonempty array")
        if "document" in job or "expectations" in job:
            raise InputError("Choose a context document or a document batch")
        reports: list[Report] = []
        remaining = self.configuration.limits.max_issues
        work = Budget(self.configuration.limits)
        for index, raw in enumerate(members):
            where = f"{job['name']}/documents/{index}"
            try:
                work.consume("work")
                member = object_value(raw, location=where)
                known_keys(member, {"document", "expectations", "base"}, location=where)
                selected = {
                    key: value for key, value in job.items() if key != "documents"
                } | member
                report = replace(self.check(selected), scope=where)
            except (OSError, UnicodeError, InputError) as error:
                report = Report(
                    where,
                    requested=1,
                    issues=(
                        Issue(
                            "input.invalid",
                            str(error),
                            where,
                            details=Location.from_error(error),
                            kind="exhausted"
                            if isinstance(error, LimitExceeded)
                            else "input_error",
                        ),
                    ),
                )
            remaining, retained = _retain_report(reports, report, remaining)
            if not retained or any(
                issue.kind == "exhausted" for issue in report.issues
            ):
                break
        count = len(members) - len(reports)
        pending = _pending_names(
            (
                f"{job['name']}/documents/{index}"
                for index in range(len(reports), len(members))
            ),
            count,
            work,
        )
        return _aggregate(str(job["name"]), reports, count, pending)

    def check(
        self,
        job: dict[str, object],
        *,
        shacl_input: ShaclJobInput | None = None,
        _owned_worker: bool = False,
    ) -> Report:
        """Validate and run one job, retaining lazy context-batch execution."""
        with self._resources.operation():
            bound_text(
                cast(str, job.get("name", "")), Budget(self.configuration.limits)
            )
            kind = check_kind(job)
            if shacl_input is not None and kind != "shacl":
                raise InputError("Runtime SHACL inputs require a SHACL job")
            if self.configuration.version == 1 and (
                kind in APPLICATION_KINDS or "depends_on" in job
            ):
                raise InputError(
                    "Application jobs and dependencies require project version 2"
                )
            if dependencies(job):
                raise InputError("Required job dependencies need project execution")
            if kind == "context" and "documents" in job:
                return self._context_batch(job)
            return run_check(
                kind,
                job,
                self._resources,
                shacl_input=shacl_input,
                _owned_worker=_owned_worker,
            )


def check_project(
    configuration: ProjectConfig | PreparedChecks,
    *,
    completed: Callable[[Report], None] | None = None,
    job_inputs: Mapping[str, ShaclJobInput] | None = None,
    _owned_worker: bool = False,
    _job_deadline: Callable[[float | None], None] | None = None,
) -> Report:
    """Retain completed sibling evidence when a required independent input fails."""
    prepared = (
        configuration
        if isinstance(configuration, PreparedChecks)
        else PreparedChecks(configuration)
    )
    config = prepared.configuration
    reports: list[Report] = []
    remaining = config.limits.max_issues
    work = Budget(config.limits)
    previous: dict[str, Report] = {}
    jobs = config.checks()
    runtime = capture_job_inputs(job_inputs, work)
    if runtime:
        known = set()
        for job in jobs:
            bound_text(cast(str, job["name"]), work)
            known.add(job["name"])
        if runtime.keys() - known:
            raise InputError("Runtime inputs must identify declared project jobs")
    for index, job in enumerate(jobs):
        name = location = f"project/checks/{index}"
        try:
            work.consume("work")
            bound_text(config.source, work)
            bound_text(cast(str, job["name"]), work)
            candidate_location = f"{config.source}/checks/{index}"
            bound_text(candidate_location, work)
            location = candidate_location
            name = cast(str, job["name"])
            blocked = _dependency_report(job, previous, work)
            timeout = job.get("timeout", 30.0)
            if (
                _job_deadline is not None
                and blocked is None
                and job.get("kind") in ("shacl", "query-rules")
            ):
                if timeout is not None and (
                    type(timeout) not in (float, int)
                    or not math.isfinite(cast(float, timeout))
                    or cast(float, timeout) <= 0
                ):
                    raise InputError(
                        "Native job timeout must be positive and finite or None"
                    )
                _job_deadline(
                    time.monotonic() + cast(float, timeout)
                    if timeout is not None
                    else None
                )
            report = (
                blocked
                if blocked is not None
                else replace(
                    prepared.check(
                        {
                            key: value
                            for key, value in job.items()
                            if key != "depends_on"
                        },
                        shacl_input=runtime.get(name),
                        _owned_worker=_owned_worker,
                    ),
                    scope=name,
                )
            )
        except (OSError, UnicodeError, InputError) as error:
            kind = "exhausted" if isinstance(error, LimitExceeded) else "input_error"
            report = Report(
                name,
                requested=1,
                issues=(
                    Issue(
                        "execution.limit_exceeded"
                        if kind == "exhausted"
                        else "input.invalid",
                        str(error),
                        location=location,
                        details=Location.from_error(error),
                        kind=cast(IssueKind, kind),
                    ),
                ),
            )
        if _job_deadline is not None:
            _job_deadline(None)
        remaining, retained = _retain_report(reports, report, remaining)
        previous[name] = reports[-1]
        if completed is not None:
            completed(reports[-1])
        if not retained or any(issue.kind == "exhausted" for issue in report.issues):
            break
    count = len(jobs) - len(reports)
    pending = _pending_names(
        (cast(str, jobs[index]["name"]) for index in range(len(reports), len(jobs))),
        count,
        work,
    )
    return _aggregate("project", reports, count, pending)
