"""Execute one ordinary project check in an owned isolated process."""

import json
import sys
import time
from pathlib import Path

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.correspondence import _ControlBudget
from bijux_ontology.reports import Issue, Location, Report
from bijux_ontology.workflows.projects.jobs import check_kind
from bijux_ontology.workflows.projects.operations import run_check
from bijux_ontology.workflows.projects.resources import ProjectResources

from .preparation import read_capture, supported


def execute(request: object) -> dict[str, object]:
    """Use one disjoint granted budget for acquisition and semantic preparation."""
    if not isinstance(request, dict) or not isinstance(request.get("limits"), dict):
        raise InputError("Invalid parallel project request")
    limits = Limits(**request["limits"])
    budget = _ControlBudget(limits, request.get("allowances"))
    issues = IssueCollector(budget)
    report: Report | None = None
    started = time.monotonic_ns()
    finished: int | None = None
    try:
        manifest, index = request.get("manifest"), request.get("index")
        if not isinstance(manifest, str) or type(index) is not int or index < 0:
            raise InputError("Invalid captured job selection")
        config, captured = read_capture(Path(manifest), budget)
        jobs = config.checks()
        if index >= len(jobs) or not supported(jobs[index]):
            raise InputError("Unsupported captured project check")
        job = dict(jobs[index])
        job.pop("depends_on", None)
        resources = ProjectResources(config, budget=budget, captured=captured)
        started = time.monotonic_ns()
        report = run_check(
            check_kind(job), job, resources, budget=budget, _owned_worker=True
        )
        finished = time.monotonic_ns()
        retain_report(report, budget, issues)
    except LimitExceeded:
        if report is not None:
            issues.retain_completed(report)
        issues.exhausted("Project job exhausted its granted operation allowance")
        report = Report(
            "project-job",
            report.checked if report is not None else 0,
            tuple(issues),
            requested=report.requested if report is not None else 1,
            complete=False,
        )
    except (InputError, ResourceError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "project.input_error",
                    "Project job input could not be qualified",
                    details=Location.from_error(error),
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Project job exhausted its granted operation allowance")
        report = Report("project-job", 0, tuple(issues), requested=1, complete=False)
    assert report is not None
    return {
        "report": report.to_dict(),
        "consumed": budget.consumed(),
        "started_ns": started,
        "finished_ns": finished if finished is not None else time.monotonic_ns(),
    }


def main() -> None:
    """Read and write one bounded protocol frame on the owned worker pipes."""
    cap = int(sys.argv[1])
    payload = sys.stdin.buffer.read(cap + 1)
    if len(payload) > cap:
        raise LimitExceeded("Parallel project request exceeds its protocol cap")
    result = execute(decode_json(payload, max_bytes=cap))
    # Metadata already qualified by retain_report or native IssueCollector. The
    # host reserves this separate output allowance before semantic execution.
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
    output = bytearray()
    for chunk in encoder.iterencode(result):
        encoded = chunk.encode()
        if len(encoded) > cap - len(output):
            raise LimitExceeded(
                "Parallel project reply exceeds its reserved output cap"
            )
        output.extend(encoded)
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
