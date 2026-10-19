"""One owned dataset plan, two native runs and reusable finite answer comparisons."""

import json
import time
from dataclasses import asdict
from threading import Event
from typing import cast

from rdflib import URIRef

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.rdf.answers.comparison import compare_answers
from bijux_ontology.rdf.answers.reading import capture_answer
from bijux_ontology.rdf.queries.access import QueryScope, _graphs, _result
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, validate_triple

from .types import (
    EngineAnswer,
    EngineConfig,
    EngineStatus,
    QualificationStatus,
    QueryBinding,
    QueryQualification,
    WorkloadQualification,
    WorkloadQuery,
)

_RDFLIB = EngineConfig("rdflib")
_PYOXIGRAPH = EngineConfig("pyoxigraph")

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def _plan(
    source: PreparedSource, scope: QueryScope, operation: Operation
) -> dict[str, object]:
    """Capture selected physical graphs once, preserving empty named inventory."""
    records = []
    namespaces: dict[str, str] = {}
    for name, graph in _graphs(source, scope, operation):
        operation.consume("resources")
        operation.consume("states")
        operation.consume("bytes", 128)
        if name is not None:
            bound_node(name, operation)
        rows = []
        for triple in graph:
            operation.consume("work")
            operation.consume("triples")
            operation.consume("states", 4)
            operation.consume("bytes", 256)
            validate_triple(*triple)
            for term in triple:
                bound_node(term, operation)
            rows.append([term_data(term) for term in triple])
        records.append([term_data(name), rows])
        for prefix, iri in graph.namespaces():
            operation.consume("work")
            bound_text(prefix, operation)
            bound_node(URIRef(iri), operation)
            text = str(iri)
            if prefix in namespaces and namespaces[prefix] != text:
                raise InputError("Selected graphs have conflicting namespace bindings")
            if prefix not in namespaces:
                operation.consume("states")
                operation.consume("bytes", 64)
                namespaces[prefix] = text
    operation.consume("states", len(namespaces))
    operation.consume("bytes", len(namespaces) * 32)
    return {
        "graphs": records,
        "namespaces": [list(pair) for pair in namespaces.items()],
    }


def _dispatch(request: dict[str, object], operation: Operation) -> dict[str, object]:
    """Reserve host adoption and forfeit delegated quota without a valid receipt."""
    request.update(
        output_cap=1,
        limits=asdict(operation.limits),
        allowances=dict.fromkeys(_KINDS, 0),
    )
    size = json_bound(request, operation)
    operation.consume("bytes", size * 3)
    cap = min(
        operation.limits.max_bytes,
        operation.remaining("bytes") // 256,
        16_384,
        operation.remaining("work") // 16,
    )
    if cap < 1024:
        raise LimitExceeded("Workload reply has no reserved adoption allowance")
    operation.consume("bytes", 64 * cap)
    operation.consume("states", 16)
    operation.consume("work", 4 * cap)
    allowances = {key: operation.remaining(key) // 4 for key in _KINDS}
    request["allowances"] = allowances
    request["output_cap"] = cap
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    received = dispatched = False
    try:
        operation.consume("work", 0)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.rdf.workloads.worker",
            payload,
            cap=max(cap, len(payload)),
            timeout=operation.deadline - time.monotonic(),
            cancel=operation.cancel,
        )
        if len(raw) > cap:
            raise ResourceError("Workload reply exceeds reserved response cap")
        response = object_value(decode_json(raw, max_bytes=cap), location="workload")
        if (
            set(response)
            != {
                "protocol",
                "status",
                "version",
                "python",
                "result",
                "message",
                "controls",
            }
            or type(response["protocol"]) is not int
            or response["protocol"] != 1
        ):
            raise ResourceError("Unsupported workload worker receipt")
        if (
            response["status"]
            not in ("completed", "unsupported", "failed", "exhausted")
            or type(response["message"]) is not str
            or type(response["version"]) is not str
            or type(response["python"]) is not str
        ):
            raise ResourceError("Invalid native workload identity or status")
        controls = object_value(response["controls"], location="controls")
        if set(controls) != set(_KINDS):
            raise ResourceError("Native workload omitted cumulative counters")
        for key in _KINDS:
            amount = controls[key]
            if type(amount) is not int or not 0 <= amount <= allowances[key]:
                raise ResourceError("Invalid native workload counter")
        # Debit a fully validated receipt without allowing a late cancellation
        # between counters to partially debit and then forfeit the same quota.
        for key in _KINDS:
            operation.shared.consume(key, cast(int, controls[key]))
        received = True
        operation.consume("work", 0)
        return response
    finally:
        if dispatched and not received:
            for key in _KINDS:
                operation.shared.consume(key, allowances[key])


def _engine(
    query: WorkloadQuery,
    config: EngineConfig,
    plan: dict[str, object],
    bindings: list[object],
    scope: QueryScope,
    max_rows: int,
    operation: Operation,
) -> EngineAnswer:
    """Preserve one native outcome without turning unsupported work into answers."""
    issues = IssueCollector(operation)
    native_version = python_version = None
    binding_policy = "rdflib-initBindings" if config.engine == "rdflib" else "SEP-0007"
    status = "failed"
    answer = None
    try:
        response = _dispatch(
            {
                "query": query.text,
                "config": asdict(config),
                "bindings": bindings,
                "max_rows": max_rows,
                **plan,
            },
            operation,
        )
        native_version = cast(str, response["version"])
        bound_text(native_version, operation)
        python_version = cast(str, response["python"])
        bound_text(python_version, operation)
        status = cast(str, response["status"])
        if status != "completed":
            message = cast(str, response["message"])
            bound_text(message, operation)
            if status == "exhausted":
                raise LimitExceeded(message)
            issues.append(
                Issue(
                    "workload.unsupported"
                    if status == "unsupported"
                    else "workload.engine_failed",
                    message,
                    query.identifier,
                    kind="unverified" if status == "unsupported" else "input_error",
                )
            )
        else:
            native = object_value(response["result"], location="query")
            size = json_bound(native, operation)
            operation.consume("bytes", 3 * size)
            raw = json.dumps(native, ensure_ascii=True, separators=(",", ":")).encode()
            result = _result(raw, scope, operation.limits, max_rows, budget=operation)
            answer = capture_answer(
                result,
                budget=operation,
                timeout=max(0.000001, operation.deadline - time.monotonic()),
                cancel=operation.cancel,
            )
            retain_report(answer.report, operation, issues)
            if answer.report.verified and answer.external_complete is True:
                return EngineAnswer(
                    config,
                    native_version,
                    python_version,
                    binding_policy,
                    "completed",
                    answer,
                    Report(
                        "rdf.workload.engine", 1, requested=1, children=(answer.report,)
                    ),
                )
            status = (
                "exhausted"
                if any(issue.kind == "exhausted" for issue in answer.report.issues)
                else "failed"
            )
    except LimitExceeded as error:
        if answer is not None:
            issues.retain_completed(answer.report)
        status = "exhausted"
        issues.exhausted(str(error), location=query.identifier)
    except InputError as error:
        status = "failed"
        try:
            issues.append(
                Issue(
                    "workload.engine_failed",
                    str(error),
                    query.identifier,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            status = "exhausted"
            issues.exhausted(str(exhausted), location=query.identifier)
    return EngineAnswer(
        config,
        native_version,
        python_version,
        binding_policy,
        cast(EngineStatus, status),
        None,
        Report(
            "rdf.workload.engine", requested=1, complete=False, issues=tuple(issues)
        ),
    )


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
        operation.consume("states", len(queries) + len(bindings))
        operation.consume("bytes", 128 * (len(queries) + len(bindings) + 1))
        identifiers = set()
        for query in queries:
            bound_text(query.identifier, operation)
            bound_text(query.text, operation)
            if query.identifier in identifiers:
                raise InputError("Workload query identifiers must be distinct")
            identifiers.add(query.identifier)
        metadata = SnapshotMetadata.capture(source, operation)
        if scope.kind == "dataset" and metadata.graph_fidelity != "complete":
            raise ResourceError(
                "Dataset workload requires complete named graph inventory"
            )
        plan = _plan(source, scope, operation)
        if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
            raise ResourceError("RDF source metadata changed during workload capture")
        captured_bindings: list[object] = []
        names = set()
        for binding in bindings:
            bound_text(binding.variable, operation)
            bound_node(binding.value, operation)
            if binding.variable in names:
                raise InputError("Workload binding variables must be distinct")
            names.add(binding.variable)
            captured_bindings.append([binding.variable, term_data(binding.value)])
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
                engine = _engine(
                    query, config, plan, captured_bindings, scope, max_rows, operation
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
