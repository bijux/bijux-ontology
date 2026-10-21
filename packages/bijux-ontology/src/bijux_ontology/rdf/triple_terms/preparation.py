"""Explicit pinned RDF 1.2 preparation and bounded actual term inspection."""

import json
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pyoxigraph as ox

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    BUDGET_KINDS,
    Budget,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.receipts import TerminalResponse
from bijux_ontology.rdf.sources.types import RdfSource

from .protocol import decode_quads, encode_quads, occurrences
from .types import (
    TRIPLE_TERM_CONTRACT,
    TripleTermInspection,
    TripleTermPreparation,
    TripleTermSnapshot,
)


class _InvalidSyntax(InputError):
    """The maintained parser rejected the supplied RDF syntax for this profile."""


def _execute(request: dict[str, object], operation: Operation) -> dict[str, object]:
    """Reserve host adoption capacity before sharing finite worker allowances."""
    request["contract"] = TRIPLE_TERM_CONTRACT
    request["limits"] = asdict(operation.limits)
    # Half of each relevant remaining allowance stays on the host for exact
    # decoding/adoption. Worker parser internals are isolated by the deadline;
    # counters cover explicit retained terms and our protocol operations.
    request["allowances"] = {kind: operation.remaining(kind) for kind in BUDGET_KINDS}
    operation.consume("bytes", json_bound(request, operation))
    terminal = TerminalResponse(
        operation,
        operation.limits.max_total_bytes,
        max_depth=operation.limits.max_depth * 4 + 16,
    )
    allowances = {
        kind: terminal.available(kind)
        // (2 if kind in ("bytes", "work", "states", "triples") else 1)
        for kind in BUDGET_KINDS
    }
    # The native evaluator returns data/counters; host adoption owns Issues.
    allowances["issues"] = 0
    delegated = DelegatedAllowance(operation, allowances)
    request["allowances"] = delegated.to_data()
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    dispatched = received = False
    try:
        terminal.admit(payload)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.rdf.triple_terms.worker",
            payload,
            cap=terminal.cap,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=operation.cancel,
        )
        response = terminal.decode(raw)
        if not isinstance(response, dict):
            raise ResourceError("Invalid triple-term worker response")
        consumed = response.get("consumed")
        if consumed is None and response.get("status") == "exhausted":
            raise LimitExceeded("Triple-term worker exceeded its protocol allowance")
        delegated.settle(consumed)
        received = True
    finally:
        try:
            terminal.close()
        finally:
            if dispatched and not received:
                delegated.forfeit()
    operation.consume("work", 0)
    if response.get("status") == "exhausted":
        raise LimitExceeded("Triple-term operation exceeded its shared allowance")
    if response.get("status") == "input_error":
        raise _InvalidSyntax("Maintained RDF parser rejected syntax for this profile")
    if response.get("status") != "result":
        raise InputError("Input is outside the selected maintained triple-term profile")
    return response


def _failure(issues: IssueCollector, error: Exception) -> None:
    """Retain a precise nongreen boundary even when its issue quota is spent."""
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error), location="rdf.triple_terms")
    else:
        try:
            issues.append(
                Issue(
                    "input.invalid"
                    if isinstance(error, _InvalidSyntax)
                    else "rdf.triple_terms_unverified",
                    str(error),
                    kind="input_error"
                    if isinstance(error, _InvalidSyntax)
                    else "unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="rdf.triple_terms")


def prepare_triple_terms(
    source: RdfSource,
    *,
    contract: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> TripleTermPreparation:
    """Parse graph-only Turtle/N-Triples under an explicit pinned CR contract.

    Text, bytes and paths are supported. Path reads and maintained parser work
    occur inside the deadline/cancellation-owned process. No network loader,
    Store insertion, generalized subject, directional literal, dataset union or
    RDF 1.1 conversion is offered. Parser/internal serializer work has a true
    process deadline; explicit inventories use cumulative operation counters.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    snapshot = None
    try:
        if not isinstance(contract, str):
            raise InputError("Triple terms require an explicit versioned contract")
        bound_text(contract, operation)
        if contract != TRIPLE_TERM_CONTRACT:
            raise InputError("Unsupported RDF triple-term contract")
        if not isinstance(source, RdfSource):
            raise InputError("Triple terms require an explicit RdfSource")
        for value in (
            source.format,
            source.document_iri,
            source.effective_url,
            source.location,
            source.role,
        ):
            if value is not None:
                bound_text(value, operation)
        if (
            source.graph_name is not None
            or source.union
            or source.compression is not None
        ):
            raise InputError("Triple terms require an uncompressed single graph source")
        if source.format not in ("turtle", "nt"):
            raise InputError("Triple terms support explicit turtle or nt syntax only")
        content = source.content
        if isinstance(content, Path):
            text, kind = str(content), "path"
            bound_text(text, operation)
        elif isinstance(content, bytes):
            if len(content) > operation.limits.max_bytes:
                raise LimitExceeded("Triple-term source exceeds max_bytes")
            operation.consume("bytes", 2 * len(content))
            operation.consume("work", len(content) + 1)
            text, kind = content.hex(), "bytes"
        elif isinstance(content, str):
            bound_text(content, operation)
            text, kind = content, "text"
        else:
            raise InputError("Triple terms support supplied text, bytes or paths only")
        response = _execute(
            {
                "action": "parse",
                "content": text,
                "kind": kind,
                "format": source.format,
                "base": source.effective_url or source.document_iri,
            },
            operation,
        )
        rows = decode_quads(response.get("rows"), operation)
        digest = response.get("sha256")
        if not isinstance(digest, str):
            raise ResourceError("Missing exact triple-term source identity")
        bound_text(digest, operation)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ResourceError("Invalid triple-term source identity")
        snapshot = TripleTermSnapshot._from_owned(
            rows, digest, source.document_iri, source.effective_url
        )
    except RecursionError:
        _failure(
            issues,
            LimitExceeded("Triple-term nesting exceeds bounded protocol capacity"),
        )
    except (InputError, ResourceError, LimitExceeded) as error:
        _failure(issues, error)
    return TripleTermPreparation(
        snapshot,
        Report(
            "rdf.triple_terms.prepare",
            int(snapshot is not None),
            tuple(issues),
            requested=1,
            complete=snapshot is not None,
            regime=TRIPLE_TERM_CONTRACT,
        ),
    )


def inspect_triple_terms(
    snapshot: TripleTermSnapshot,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> TripleTermInspection:
    """Read actual nested occurrences under the caller's current finite scope."""
    if not isinstance(snapshot, TripleTermSnapshot):
        raise InputError("Inspection requires a factory-owned triple-term snapshot")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Triple-term limits conflict with the shared budget")
    operation = budget or Budget(limits)
    issues = IssueCollector(operation)
    terms: tuple[ox.Triple, ...] = ()
    complete = False
    try:
        for value in (
            snapshot.contract,
            snapshot.source_sha256,
            snapshot.document_iri,
            snapshot.effective_url,
        ):
            if value is not None:
                bound_text(value, operation)
        encode_quads(snapshot.quads, operation)
        terms = occurrences(snapshot.quads, operation)
        complete = True
    except RecursionError:
        _failure(
            issues,
            LimitExceeded("Triple-term nesting exceeds bounded protocol capacity"),
        )
    except (InputError, LimitExceeded) as error:
        _failure(issues, error)
    return TripleTermInspection(
        snapshot,
        terms,
        Report(
            "rdf.triple_terms.inspect",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime=TRIPLE_TERM_CONTRACT,
        ),
    )
