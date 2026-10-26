"""One actual native query observation without workload comparison policy."""

import json
import time
from dataclasses import asdict
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    BudgetKind,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.answers.reading import capture_answer
from bijux_ontology.rdf.queries.results import adopt_result

from .capture import wire_input
from .types import EngineAnswer, EngineConfig, EngineStatus, NativeQueryInput

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


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
    delegated = DelegatedAllowance(operation, allowances)
    request["allowances"] = delegated.to_data()
    request["output_cap"] = cap
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    received = dispatched = False
    try:
        operation.consume("work", 0)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.rdf.queries.native.worker",
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
        delegated.settle(response["controls"])
        received = True
        operation.consume("work", 0)
        return response
    finally:
        if dispatched and not received:
            delegated.forfeit()


def observe_query(
    identifier: str,
    text: str,
    config: EngineConfig,
    selected: NativeQueryInput,
    max_rows: int,
    operation: Operation,
) -> EngineAnswer:
    """Preserve one native outcome without turning unsupported work into answers."""
    if type(selected) is not NativeQueryInput or type(config) is not EngineConfig:
        raise InputError("Native observation requires qualified input and typed engine")
    if (
        type(identifier) is not str
        or not identifier
        or type(text) is not str
        or not text
    ):
        raise InputError("Native observation requires an identifier and SPARQL text")
    if type(max_rows) is not int or max_rows <= 0:
        raise InputError("Native observation requires a positive row bound")
    issues = IssueCollector(operation)
    native_version = python_version = None
    binding_policy = "rdflib-initBindings" if config.engine == "rdflib" else "SEP-0007"
    status = "failed"
    answer = None
    try:
        bound_text(identifier, operation)
        bound_text(text, operation)
        if not text.strip():
            raise InputError("Native observation requires SPARQL text")
        response = _dispatch(
            {
                "query": text,
                "config": asdict(config),
                "max_rows": max_rows,
                **wire_input(selected, operation),
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
                    identifier,
                    kind="unverified" if status == "unsupported" else "input_error",
                )
            )
        else:
            native = object_value(response["result"], location="query")
            size = json_bound(native, operation)
            operation.consume("bytes", 3 * size)
            raw = json.dumps(native, ensure_ascii=True, separators=(",", ":")).encode()
            result = adopt_result(
                raw, selected.scope, operation.limits, max_rows, budget=operation
            )
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
        issues.exhausted(str(error), location=identifier)
    except InputError as error:
        status = "failed"
        try:
            issues.append(
                Issue(
                    "workload.engine_failed",
                    str(error),
                    identifier,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            status = "exhausted"
            issues.exhausted(str(exhausted), location=identifier)
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
