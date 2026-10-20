"""One concrete installed worker dispatch with shared host controls and raw reports."""

import base64
import json
import time
from dataclasses import asdict
from threading import Event

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report, report_from_data
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, BudgetKind, IssueCollector, Limits
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.records.codecs import term_data, witness_data

from .preparation import reserve
from .types import (
    ContextRequest,
    EmissionRequest,
    OperationResult,
    ReproductionRequest,
    ShaclRequest,
    SkosRequest,
    SourceObligation,
)

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def request_data(request: ReproductionRequest, budget: Budget) -> dict[str, object]:
    """Encode only the four owned profiles through existing exact RDF codecs."""
    if type(request) not in (
        ContextRequest,
        EmissionRequest,
        SkosRequest,
        ShaclRequest,
    ):
        raise InputError("Execution requires an owned reproduction request")
    reserve(budget)
    data: dict[str, object] = {"kind": request.kind}
    if isinstance(request, ContextRequest | EmissionRequest):
        data.update(
            document=request.document_json,
            base=request.base,
            resources=[asdict(resource) for resource in request.resources],
        )
        if isinstance(request, ContextRequest):
            data["expectations"] = request.expectations_json
        else:
            data.update(models=request.models_json, usages=request.usages_json)
    elif isinstance(request, SkosRequest):
        data.update(
            source=witness_data(request.source, budget),
            concepts=list(request.concepts),
            rules=list(request.rules),
        )
    else:
        data.update(
            data=witness_data(request.data, budget),
            shapes=witness_data(request.shapes, budget),
            focus=[term_data(term, budget) for term in request.focus_nodes],
            roots=[term_data(term, budget) for term in request.use_shapes],
            imports=[],
        )
        imports = []
        for resource in request.imports:
            budget.consume("bytes", 4 * len(resource.content) + 128)
            budget.consume("work", len(resource.content))
            imports.append(
                {
                    "iri": resource.iri,
                    "content": base64.b64encode(resource.content).decode("ascii"),
                    "format": resource.format,
                    "document_iri": resource.document_iri,
                }
            )
        data["imports"] = imports
    size = json_bound(data, budget)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Reproduction request exceeds max_bytes")
    budget.consume("bytes", size)
    return data


def _response(
    raw: bytes,
    budget: Operation,
    delegated: DelegatedAllowance,
    capacity: int,
    request: ReproductionRequest,
) -> OperationResult:
    response = object_value(
        decode_json(raw, max_bytes=capacity, max_depth=budget.limits.max_depth),
        location="worker response",
    )
    if set(response) != {"report", "obligations", "findings", "consumed"}:
        raise ResourceError("Malformed reproduction worker response")
    receipt = delegated.settle(response["consumed"])
    report = report_from_data(response["report"])
    budget.shared._register_transferred_report(
        report,
        paid_issues=receipt["issues"],
        max_references=len(raw) * 2 + 1,
    )
    raw_obligations = response["obligations"]
    if not isinstance(raw_obligations, list):
        raise ResourceError("Malformed native source obligations")
    obligations = []
    for item in raw_obligations:
        value = object_value(item, location="worker obligation")
        if set(value) != {"path", "subjects", "graphs", "raw_json", "named"}:
            raise ResourceError("Malformed native source obligation fields")
        path, subjects, graphs = value["path"], value["subjects"], value["graphs"]
        if (
            not isinstance(path, list)
            or any(
                type(part) not in (str, int) or type(part) is int and part < 0
                for part in path
            )
            or not isinstance(subjects, list)
            or any(type(part) is not str for part in subjects)
            or not isinstance(graphs, list)
            or any(part is not None and type(part) is not str for part in graphs)
            or value["raw_json"] is not None
            and type(value["raw_json"]) is not str
            or type(value["named"]) is not bool
        ):
            raise ResourceError("Invalid native source obligation scalar kinds")
        obligations.append(
            SourceObligation(
                tuple(path),
                tuple(subjects),
                tuple(graphs),
                value["raw_json"],
                value["named"],
            )
        )
    findings = response["findings"]
    if findings is not None and type(findings) is not str:
        raise ResourceError("Malformed native SHACL finding identity")
    return OperationResult._owned(
        _request=request,
        report=report,
        obligations=tuple(obligations),
        shacl_findings_json=findings,
    )


def _run(request: ReproductionRequest, operation: Operation) -> OperationResult:
    """Dispatch once; callers own already-proved best evidence before this stage."""
    data = request_data(request, operation)
    envelope: dict[str, object] = {
        "request": data,
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in _KINDS},
    }
    size = json_bound(envelope, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Reproduction protocol exceeds max_bytes")
    # Encode only after its conservative bound is reserved. Counter widths can
    # only decrease when the final shared allowances replace this first envelope.
    operation.consume("bytes", 2 * size)
    operation.consume("work", size)
    payload = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    capacity = min(operation.limits.max_bytes, max(len(payload), 4096) + 4096)
    # Reserve host decoding, reference ownership and terminal report adoption
    # before the worker receives its remaining allowance.
    reserve(operation, capacity * 2 + 1)
    operation.consume("bytes", 6 * capacity)
    allowances = {kind: operation.remaining(kind) for kind in _KINDS}
    delegated = DelegatedAllowance(operation, allowances)
    envelope["allowances"] = delegated.to_data()
    payload = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Reproduction exceeded its wall-clock deadline")
    try:
        raw = execute_worker(
            "bijux_ontology.workflows.reproductions.worker",
            payload,
            cap=capacity,
            timeout=remaining,
            cancel=operation.cancel,
        )
        return _response(raw, operation, delegated, capacity, request)
    except InputError as error:
        # No qualified receipt means no unused delegated allowance can be
        # recovered. This is conservative forfeiture, never a native work count.
        # Terminal accounting bypasses the expired/cancelled operation checkpoint.
        delegated.forfeit()
        raise LimitExceeded(
            "Reproduction stopped without a qualified worker control receipt; "
            "the remaining delegated allowance was forfeited: " + str(error)
        ) from error


def run_reproduction(
    request: ReproductionRequest,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> OperationResult:
    """Rerun an ordinary supported public operation in the installed runtime."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    try:
        return _run(request, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "reproduction.worker_failed"
                    if isinstance(error, ResourceError)
                    else "reproduction.input",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return OperationResult._owned(
        _request=request,
        report=Report(
            "reproduction.execution", requested=1, complete=False, issues=tuple(issues)
        ),
        obligations=(),
        shacl_findings_json=None,
    )
