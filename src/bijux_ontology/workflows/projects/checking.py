"""Reuse explicit local resources while retaining every selected check outcome."""

from collections.abc import Callable
from dataclasses import replace
from typing import cast

from bijux_ontology._json import known_keys, object_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Issue, IssueKind, Report, combine_reports
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.jobs import check_kind
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
            )
        )
        return 0, False
    reports.append(report)
    return remaining - len(report.issues), True


def _aggregate(scope: str, reports: list[Report], pending: tuple[str, ...]) -> Report:
    result = combine_reports(scope, *reports)
    if not pending:
        return result
    issues = list(result.issues)
    for index in range(len(issues) - 1, -1, -1):
        if issues[index].kind == "exhausted":
            issues[index] = replace(
                issues[index],
                message=issues[index].message
                + f"; {len(pending)} required scopes were not started",
                expected=", ".join(pending),
            )
            break
    return replace(
        result,
        issues=tuple(issues),
        requested=(result.requested or 0) + len(pending),
        complete=False,
    )


class PreparedChecks:
    """Own reused vocabulary and context resources for one explicit project.

    No global caches or implicit discovery are used. A cached vocabulary owns
    its graphs; files changed after first use require a new prepared project.
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
        pending = tuple(
            f"{job['name']}/documents/{index}"
            for index in range(len(reports), len(members))
        )
        return _aggregate(str(job["name"]), reports, pending)

    def check(self, job: dict[str, object]) -> Report:
        """Validate and run one job, retaining lazy context-batch execution."""
        kind = check_kind(job)
        if kind == "context" and "documents" in job:
            return self._context_batch(job)
        return run_check(kind, job, self._resources)


def check_project(
    configuration: ProjectConfig | PreparedChecks,
    *,
    completed: Callable[[Report], None] | None = None,
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
    jobs = config.checks()
    for index, job in enumerate(jobs):
        name = cast(str, job["name"])
        location = f"{config.source}/checks/{index}"
        try:
            work.consume("work")
            report = replace(prepared.check(job), scope=name)
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
                        kind=cast(IssueKind, kind),
                    ),
                ),
            )
        remaining, retained = _retain_report(reports, report, remaining)
        if completed is not None:
            completed(reports[-1])
        if not retained or any(issue.kind == "exhausted" for issue in report.issues):
            break
    pending = tuple(cast(str, job["name"]) for job in jobs[len(reports) :])
    return _aggregate("project", reports, pending)
