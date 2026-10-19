"""Actual prepared-record validation with bounded full diagnostic delivery."""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Event
from uuid import uuid4

from bijux_ontology._jsonpaths import bound_path
from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import SubjectIdentity
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, BudgetKind, Limits, _issue_metadata
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.preparation import PreparedModels
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Outcome, Report, combine_reports

from .types import (
    CheckCoverage,
    FindingEvent,
    FindingSink,
    FindingStreamLimits,
    FindingStreamSummary,
    RecordResultEvent,
    StreamEvent,
)

_DEFAULT_STREAM_LIMITS = FindingStreamLimits()


class _RecordBudget(Budget):
    """Keep legacy issue caps local while sharing every other run allowance."""

    def __init__(self, limits: Limits, operation: Operation) -> None:
        """Own only this record's report and per-record issue identities."""
        super().__init__(limits)
        self.operation = operation

    def remaining(self, kind: BudgetKind) -> int:
        """Refuse work before either the per-record or cumulative limit is reached."""
        amount = super().remaining(kind)
        return (
            min(
                amount, self.operation.limits.max_issues - self._counts.get("issues", 0)
            )
            if kind == "issues"
            else min(amount, self.operation.remaining(kind))
        )

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Never bypass per-record max_issues or reset cumulative engine work."""
        if type(amount) is not int or amount < 0:
            raise InputError("Budget consumption requires a nonnegative integer")
        if amount > self.remaining(kind):
            raise LimitExceeded(
                "Prepared record exceeded its ordinary per-record limit"
            )
        if kind == "issues":
            self.operation.consume("work", 0)
        else:
            self.operation.consume(kind, amount)
        super().consume(kind, amount)


@dataclass
class _Totals:
    """Retain bounded counters and samples, never the complete report corpus."""

    run_id: str
    seen: int = 0
    evaluated: int = 0
    completed: int = 0
    failing: int = 0
    checked: int = 0
    requested: int = 0
    findings: int = 0
    emitted: int = 0
    output: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    samples: list[FindingEvent] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    all_retained: bool = True

    def summary(
        self,
        *,
        semantic_complete: bool,
        export_complete: bool,
        failure: Issue | None = None,
    ) -> FindingStreamSummary:
        """Derive raw semantics from observed outcomes, never displayed samples."""
        if self.outcomes.get("input_error", 0):
            outcome: Outcome = "input_error"
        elif self.outcomes.get("exhausted", 0):
            outcome = "exhausted"
        elif self.failing:
            outcome = "disagreement"
        elif not semantic_complete:
            outcome = "unverified"
        else:
            outcome = "agreement"
        report = (
            combine_reports("models.finding_stream", *self.reports)
            if self.all_retained and self.evaluated and export_complete
            else None
        )
        return FindingStreamSummary(
            self.run_id,
            self.seen,
            self.evaluated,
            self.completed,
            self.failing,
            self.checked,
            self.requested,
            self.findings,
            tuple(sorted(self.kinds.items())),
            tuple(sorted(self.outcomes.items())),
            self.emitted,
            self.output,
            tuple(self.samples),
            semantic_complete,
            export_complete,
            outcome,
            failure,
            report,
        )


def _coverage(
    report: Report, operation: Operation
) -> tuple[list[tuple[tuple[int, ...], Report]], tuple[CheckCoverage, ...]]:
    """Preserve actual parent paths and unknown descendants without inventing checks."""
    operation.consume("bytes", 256)
    pending: list[tuple[tuple[int, ...], Report]] = [((), report)]
    nodes: list[tuple[tuple[int, ...], Report]] = []
    coverage: list[CheckCoverage] = []
    while pending:
        operation.consume("work")
        path, current = pending.pop()
        if len(path) > operation.limits.max_depth:
            raise LimitExceeded("Finding report scope exceeds max_depth")
        operation.consume("states")
        operation.consume("bytes", 256 + 16 * len(path))
        bound_text(current.scope, operation)
        bound_text(current.regime, operation)
        nodes.append((path, current))
        coverage.append(
            CheckCoverage(
                path,
                current.scope,
                current.checked,
                current.requested or 0,
                current.complete,
                current.outcome,
                current.regime,
            )
        )
        operation.consume("bytes", 128 * len(current.children))
        for index in range(len(current.children) - 1, -1, -1):
            operation.consume("work")
            pending.append(((*path, index), current.children[index]))
    return nodes, tuple(coverage)


def _data(event: StreamEvent, operation: Operation) -> dict[str, object]:
    """Reserve typed frame metadata before invoking ordinary JSON serialization."""
    operation.consume("bytes", 1024)
    if isinstance(event, FindingEvent):
        _issue_metadata(event.issue, operation)
        operation.consume("bytes", 128 + 64 * len(event.issue.evidence_json or ""))
        return {
            "type": "finding",
            "run_id": event.run_id,
            "record_index": event.record_index,
            "finding_index": event.finding_index,
            "check_paths": event.check_paths,
            "issue": event.issue.to_dict(),
        }
    if isinstance(event, RecordResultEvent):
        operation.consume("bytes", 512 * len(event.checks))
        return {
            "type": "record",
            "run_id": event.run_id,
            "record_index": event.record_index,
            "checks": [
                {
                    "path": item.path,
                    "scope": item.scope,
                    "checked": item.checked,
                    "requested": item.requested,
                    "complete": item.complete,
                    "outcome": item.outcome,
                    "regime": item.regime,
                }
                for item in event.checks
            ],
        }
    return {
        "type": "summary",
        "run_id": event.run_id,
        "records_seen": event.records_seen,
        "records_evaluated": event.records_evaluated,
        "records_complete": event.records_complete,
        "failing_records": event.failing_records,
        "checked": event.checked,
        "requested": event.requested,
        "finding_count": event.finding_count,
        "finding_counts": event.finding_counts,
        "finding_counts_complete": event.finding_counts_complete,
        "record_outcomes": event.record_outcomes,
        "emitted_findings": event.emitted_findings,
        "semantic_complete": event.semantic_complete,
        "selected_frames_finished": True,
        "raw_outcome": event.raw_outcome,
    }


def _send(
    event: StreamEvent,
    sink: FindingSink,
    totals: _Totals,
    selected: FindingStreamLimits,
    operation: Operation,
) -> None:
    """Publish one bounded complete frame, applying synchronous backpressure."""
    if (
        isinstance(event, FindingEvent)
        and totals.emitted >= selected.max_emitted_findings
    ):
        raise LimitExceeded("Finding stream exceeded max_emitted_findings")
    data = _data(event, operation)
    size = json_bound(data, operation)
    operation.consume("bytes", 2 * size + 1)
    payload = (
        json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if (
        len(payload) > operation.limits.max_bytes
        or len(payload) > selected.max_output_bytes - totals.output
    ):
        raise LimitExceeded("Finding stream exceeded its output byte cap")
    operation.consume("work", 0)
    sink.write(event, payload)
    totals.output += len(payload)
    if isinstance(event, FindingEvent):
        totals.emitted += 1
    operation.consume("work", 0)


def stream_model_findings(
    prepared: PreparedModels,
    documents: Iterable[object],
    usages: Sequence[ModelUsage],
    *,
    sink: FindingSink,
    stream_limits: FindingStreamLimits = _DEFAULT_STREAM_LIMITS,
    sample_codes: tuple[str, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> FindingStreamSummary:
    """Check actual prepared records and deliver their complete selected diagnostics.

    Each actual check keeps its ordinary max_issues and per-record Limits.
    A smaller explicit run max_issues also caps each record. All other work
    shares one cumulative Operation. This explicit mode has separate emitted,
    retained and output caps. JSON-LD/PyLD, producers and sinks retain ordinary
    in-process cooperative lifetimes.
    No arbitrary validation callback, SHACL incremental streaming, hard callback
    preemption or global corpus consistency is claimed. Unknown or incomplete
    descendants remain incomplete; reaching EOF and finishing the full export
    are separate requirements. Sample filtering changes presentation only.
    """
    operation = Operation(limits, budget, timeout, cancel)
    totals = _Totals(uuid4().hex)
    semantic_complete = False
    try:
        if type(prepared) is not PreparedModels:
            raise InputError("Finding streams require an ordinary PreparedModels plan")
        if type(stream_limits) is not FindingStreamLimits:
            raise InputError("Expected FindingStreamLimits")
        stream_limits = FindingStreamLimits(
            stream_limits.max_records,
            stream_limits.max_emitted_findings,
            stream_limits.max_retained,
            stream_limits.max_output_bytes,
        )
        for name in (
            "max_records",
            "max_emitted_findings",
            "max_retained",
            "max_output_bytes",
        ):
            value = getattr(stream_limits, name)
            if type(value) is not int or value < (
                0 if name in ("max_retained", "max_emitted_findings") else 1
            ):
                raise InputError(
                    "Finding stream limits require bounded integer capacities"
                )
        if isinstance(documents, str | bytes | Mapping):
            raise InputError(
                "Finding streams require an iterable of independent documents"
            )
        if not callable(getattr(sink, "write", None)) or not callable(
            getattr(sink, "finish", None)
        ):
            raise InputError("Finding streams require a typed synchronous sink")
        candidate: object = usages
        if not isinstance(candidate, Sequence) or isinstance(candidate, str | bytes):
            raise InputError("Finding streams require finite model usages")
        count = len(usages)
        operation.consume("states", count)
        operation.consume("bytes", 128 + 1024 * count)
        captured_bindings = []
        for index in range(count):
            operation.consume("work")
            usage = usages[index]
            if type(usage) is not ModelUsage:
                raise InputError("Finding streams require ordinary model usages")
            model, path, subject, reference, graph, entity, closed = (
                usage.model,
                usage.path,
                usage.subject,
                usage.reference,
                usage.graph,
                usage.entity,
                usage.closed,
            )
            bound_text(model, operation)
            bound_path(path, operation)
            for text in (reference, graph):
                if text is not None and text is not ABSENT:
                    bound_text(text, operation)
            if subject is not None:
                if type(subject) is not SubjectIdentity:
                    raise InputError(
                        "Finding streams require ordinary subject identity"
                    )
                expected, allow_blank = subject.expected_iri, subject.allow_blank
                if expected is not None:
                    bound_text(expected, operation)
                subject = SubjectIdentity(expected, allow_blank)
            captured_bindings.append(
                ModelUsage(
                    model,
                    path,
                    subject=subject,
                    reference=reference,
                    graph=graph,
                    entity=entity,
                    closed=closed,
                )
            )
        bindings = tuple(captured_bindings)
        if type(sample_codes) is not tuple or any(
            type(code) is not str for code in sample_codes
        ):
            raise InputError("Sample code filters require an explicit tuple of strings")
        for code in sample_codes:
            bound_text(code, operation)
        iterator = iter(documents)
        sentinel = object()
        while True:
            operation.consume("work")
            document = next(iterator, sentinel)
            if document is sentinel:
                semantic_complete = (
                    totals.evaluated > 0 and totals.completed == totals.evaluated
                )
                break
            totals.seen += 1
            if totals.evaluated >= stream_limits.max_records:
                raise LimitExceeded("Finding stream left an unchecked record tail")
            operation.consume("states")
            operation.consume("bytes", 1024)
            record_index = totals.evaluated
            handoff = min(
                stream_limits.max_retained - len(totals.samples),
                prepared.limits.max_issues + 1,
            )
            operation.consume("bytes", 128 + 512 * handoff)
            operation.consume("work", handoff + 1)
            record = prepared.check(
                document, bindings, budget=_RecordBudget(prepared.limits, operation)
            )
            # Reserve bounded sample handoff before checking, so a late stop
            # cannot erase a paid finding merely because projection cannot run.
            for index in range(min(handoff, len(record.issues))):
                issue = record.issues[index]
                if not sample_codes or issue.code in sample_codes:
                    totals.samples.append(
                        FindingEvent(totals.run_id, record_index, index, issue, ((),))
                    )
            # These constant-size counters preserve already-known raw truth even
            # if subsequent scope projection or the first sink write cannot run.
            totals.evaluated += 1
            totals.completed += int(record.complete)
            totals.checked += record.checked
            totals.requested += record.requested or 0
            totals.findings += len(record.issues)
            totals.failing += int(any(issue.kind == "finding" for issue in record))
            totals.outcomes[record.outcome] = totals.outcomes.get(record.outcome, 0) + 1
            operation.consume("work", len(record.issues))
            for issue in record:
                totals.kinds[issue.kind] = totals.kinds.get(issue.kind, 0) + 1
            if (
                totals.all_retained
                and len(totals.reports) < stream_limits.max_retained
                and totals.findings <= stream_limits.max_retained
            ):
                operation.consume("bytes", 128)
                totals.reports.append(record)
            else:
                totals.all_retained = False
                totals.reports.clear()
            nodes, coverage = _coverage(record, operation)
            _send(
                RecordResultEvent(totals.run_id, record_index, coverage),
                sink,
                totals,
                stream_limits,
                operation,
            )
            for index, issue in enumerate(record):
                operation.consume("work")
                operation.consume("bytes", 256 + 64 * len(nodes))
                paths = []
                for path, node in nodes:
                    operation.consume("work", len(node.issues) + 1)
                    if any(item is issue for item in node):
                        paths.append(path)
                event = FindingEvent(
                    totals.run_id, record_index, index, issue, tuple(paths)
                )
                _send(event, sink, totals, stream_limits, operation)
            if any(issue.kind == "exhausted" for issue in record):
                raise LimitExceeded(
                    "A prepared record exhausted its ordinary engine limits"
                )
        if not totals.evaluated:
            raise InputError("No records were checked in the required finding stream")
        # A footer confirms finished selected frames, never its own future
        # finishing. Local confirmation follows the second successful finish.
        sink.finish()
        operation.consume("work", 0)
        pending = totals.summary(
            semantic_complete=semantic_complete, export_complete=False
        )
        _send(pending, sink, totals, stream_limits, operation)
        sink.finish()
        operation.consume("work", 0)
        return totals.summary(semantic_complete=semantic_complete, export_complete=True)
    except Exception as error:  # noqa: BLE001 - producer/sink failures are returned with observed raw totals
        exhausted = isinstance(error, LimitExceeded)
        # Formatting OSError or a caller-defined exception can allocate its
        # entire message. Retain only already-owned scalar argument prefixes.
        arguments = error.args
        text = next((value for value in arguments[:2] if type(value) is str), "")
        message = type(error).__name__[:100] + (": " + text[:1900] if text else "")
        failure = Issue(
            "finding_stream.exhausted" if exhausted else "finding_stream.failed",
            message,
            kind="exhausted" if exhausted else "input_error",
        )
        return totals.summary(
            semantic_complete=semantic_complete, export_complete=False, failure=failure
        )
