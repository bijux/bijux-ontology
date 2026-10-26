"""Two native observations and caller-selected finite bag/set comparison policy."""

import time
from threading import Event
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.answers.comparison import compare_answers
from bijux_ontology.rdf.queries.native.capture import capture_selected_input
from bijux_ontology.rdf.queries.native.observation import observe_query
from bijux_ontology.rdf.queries.native.types import EngineConfig, QueryBinding
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource

from .types import (
    QualificationStatus,
    QueryQualification,
    WorkloadQualification,
    WorkloadQuery,
)

_RDFLIB = EngineConfig("rdflib")
_PYOXIGRAPH = EngineConfig("pyoxigraph")


def qualify_workload(
    source: PreparedSource,
    queries: tuple[WorkloadQuery, ...],
    *,
    scope: QueryScope,
    left: EngineConfig = _RDFLIB,
    right: EngineConfig = _PYOXIGRAPH,
    bindings: tuple[QueryBinding, ...] = (),
    max_rows: int = 1000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> WorkloadQualification:
    """Qualify only supplied finite answer bags/sets under both exact configurations.

    Asserted RDF, explicit default/named graphs and selected native parameter
    APIs are recorded. ORDER BY tie order is outside bag/set qualification.
    Nondeterministic functions, slices, extensions and unsupported native source
    fidelity remain unqualified. Results do not certify other queries/runtimes.
    """
    if type(source) is not PreparedSource or not isinstance(scope, QueryScope):
        raise InputError("Workloads require an owned PreparedSource and QueryScope")
    if type(queries) is not tuple or any(
        type(query) is not WorkloadQuery for query in queries
    ):
        raise InputError("Workloads require a finite tuple of WorkloadQuery")
    if type(bindings) is not tuple or any(
        type(binding) is not QueryBinding for binding in bindings
    ):
        raise InputError("Workload bindings require a finite tuple of QueryBinding")
    if (
        not isinstance(left, EngineConfig)
        or not isinstance(right, EngineConfig)
        or type(max_rows) is not int
        or max_rows <= 0
    ):
        raise InputError("Workloads require typed engines and a positive row bound")
    if left.engine == right.engine:
        raise InputError("Choose two distinct maintained workload adapters")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    results: list[QueryQualification] = []
    children: list[Report] = []
    current_reports: list[Report] = []
    metadata = None
    try:
        operation.consume("states", len(queries))
        operation.consume("bytes", 128 * (len(queries) + 1))
        identifiers = set()
        for query in queries:
            bound_text(query.identifier, operation)
            bound_text(query.text, operation)
            if query.identifier in identifiers:
                raise InputError("Workload query identifiers must be distinct")
            identifiers.add(query.identifier)
        metadata = SnapshotMetadata.capture(source, operation)
        selected = capture_selected_input(source, scope, bindings, operation)
        if not metadata.same_scope(selected.source_metadata):
            raise ResourceError("RDF source metadata changed during workload capture")
        if not queries:
            issues.append(
                Issue(
                    "workload.empty",
                    "No actual supplied queries establish qualification",
                    kind="unverified",
                )
            )
        for query in queries:
            operation.consume("states", 8)
            operation.consume("bytes", 512)
            current_reports = []
            engines_list = []
            local = IssueCollector(operation)
            for config in (left, right):
                engine = observe_query(
                    query.identifier, query.text, config, selected, max_rows, operation
                )
                current_reports.append(engine.report)
                engines_list.append(engine)
            engines = tuple(engines_list)
            for engine in engines:
                retain_report(engine.report, operation, local)
            reports = current_reports
            if all(engine.status == "completed" for engine in engines):
                before, after = engines[0].answer, engines[1].answer
                assert before is not None and after is not None
                proof = compare_answers(
                    before,
                    after,
                    semantics=query.comparison,
                    require_external_complete=True,
                    budget=operation,
                    timeout=max(0.000001, operation.deadline - time.monotonic()),
                    cancel=cancel,
                )
                reports.append(proof)
                retain_report(proof, operation, local)
                status = (
                    "agreeing"
                    if proof.verified
                    else "disagreeing"
                    if proof.complete
                    else "exhausted"
                    if any(issue.kind == "exhausted" for issue in proof.issues)
                    else "unsupported"
                )
            else:
                status = (
                    "exhausted"
                    if any(engine.status == "exhausted" for engine in engines)
                    else "failed"
                    if any(engine.status == "failed" for engine in engines)
                    else "unsupported"
                )
            report = Report(
                "rdf.workload.query",
                int(status in ("agreeing", "disagreeing")),
                tuple(local),
                requested=1,
                complete=status in ("agreeing", "disagreeing"),
                children=tuple(reports),
                regime="answer-" + query.comparison,
            )
            current_reports.append(report)
            retain_report(report, operation, issues)
            results.append(
                QueryQualification(
                    query, engines, cast(QualificationStatus, status), report
                )
            )
            children.append(report)
    except LimitExceeded as error:
        for current in current_reports:
            issues.retain_completed(current)
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "workload.unqualified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return WorkloadQualification(
        scope,
        metadata,
        tuple(results),
        Report(
            "rdf.workload",
            len(results),
            tuple(issues),
            requested=max(1, len(queries)),
            complete=bool(queries)
            and len(results) == len(queries)
            and all(result.report.complete for result in results),
            children=tuple(children),
            regime="supplied-local-answer-workload",
        ),
    )
