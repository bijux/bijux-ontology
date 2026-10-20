"""Pull one independent document at a time with bounded retained stream truth."""

import threading
from collections.abc import Iterable, Iterator, Mapping
from typing import Self

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.values import decode_json
from bijux_ontology.jsonld.processing.preparation import (
    JSONLDPreparation,
    prepare_jsonld,
)
from bijux_ontology.jsonld.processing.resources import DocumentLoader, context_resources
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import JSONLDRecord, JSONLDRecordResult, RecordCheck


def _failure(error: InputError, operation: Budget, location: str) -> Report:
    """Bound diagnostics before constructing a failed record, retaining no source."""
    issues = IssueCollector(operation)
    if isinstance(error, LimitExceeded):
        issues.exhausted(
            "Record preparation exceeded selected limits", location=location
        )
    else:
        try:
            message = str(error)
            bound_text(message, operation)
            issues.append(
                Issue(
                    "jsonld.record_unavailable"
                    if isinstance(error, ResourceError)
                    else "input.invalid",
                    message,
                    location,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded:
            issues.exhausted(
                "Record diagnostic exceeded selected limits", location=location
            )
    return Report(
        "jsonld.record",
        0,
        tuple(issues),
        requested=1,
        complete=False,
        regime="independent-jsonld-document",
    )


class JSONLDRecordStream(Iterator[JSONLDRecordResult]):
    """Pull explicit frames without prefetch or retained input/result history.

    Record limits govern each independent preparation; the callback receives
    that budget and owns whether its own native calls share those counters.
    Stream limits separately bound resource capture and summary retention; they
    do not claim a cumulative instruction limit over all per-record operations.
    Unchanged context responses are captured once within this stream. A callback
    runs in process and receives the actual prepared source and record budget.
    Neither caller iteration, callbacks nor PyLD are forcibly preempted. Closing
    does not close caller-owned producers. No graph union or global data check is
    implied by successful independent records.
    """

    def __init__(
        self,
        records: Iterable[JSONLDRecord],
        *,
        document_loader: DocumentLoader | None = None,
        record_limits: Limits | None = None,
        limits: Limits | None = None,
        max_records: int = 1000,
        check: RecordCheck | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Prepare only bounded policy/resource ownership, never read a frame."""
        if isinstance(records, str | bytes | Mapping) or not isinstance(
            records, Iterable
        ):
            raise InputError(
                "A record stream requires an iterable of JSONLDRecord frames"
            )
        if (
            limits is not None
            and not isinstance(limits, Limits)
            or record_limits is not None
            and not isinstance(record_limits, Limits)
        ):
            raise InputError("Record and stream policies require Limits")
        if (
            type(max_records) is not int
            or not 0 < max_records <= (limits or Limits()).max_states
        ):
            raise InputError(
                "max_records requires a positive integer within max_states"
            )
        if check is not None and not callable(check):
            raise InputError("A record check requires an ordinary callable")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise InputError("Record cancellation requires a threading.Event")
        self._resources = context_resources(document_loader, limits=limits)
        self._operation = self._resources.budget
        self._record_limits = record_limits or self._operation.limits
        self._iterator: Iterator[JSONLDRecord] | None = iter(records)
        self._max_records, self._check = max_records, check
        self._cancel = cancel or threading.Event()
        self._issues = IssueCollector(self._operation)
        self._current: JSONLDRecordResult | None = None
        self._current_adopted = False
        self._processed = 0
        self._finished = False
        self._closed = False
        self._pulling = False

    @property
    def processed(self) -> int:
        """Count actual yielded records, including invalid independent frames."""
        return self._processed

    @property
    def finished(self) -> bool:
        """Whether the actual producer ended, independently of record conformance."""
        return self._finished

    @property
    def last_result(self) -> JSONLDRecordResult | None:
        """Inspect the sole completed slot, including a cancelled admitted pull."""
        return self._current

    @property
    def retained_records(self) -> int:
        """Expose the sole current-result slot; caller-held results are independent."""
        return int(self._current is not None)

    @property
    def report(self) -> Report:
        """State actual completion without inventing coverage of the unseen tail."""
        return Report(
            "jsonld.records",
            self._processed,
            tuple(self._issues),
            requested=max(1, self._processed + int(not self._finished)),
            complete=self._finished,
            children=(self._current.report,)
            if self._current is not None and self._current_adopted
            else (),
            regime="independent-jsonld-records",
            exclusions=("cross-record-obligations",),
        )

    def __iter__(self) -> "JSONLDRecordStream":
        """Keep the iterator single-pass without consuming its producer."""
        return self

    def __enter__(self) -> Self:
        """Use deterministic cooperative cleanup for partial consumption."""
        if self._closed:
            raise InputError("A closed record stream cannot be reopened")
        return self

    def __exit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> None:
        """Mark an unchecked tail without suppressing caller errors."""
        self.close()

    def cancel(self) -> None:
        """Signal cooperative cancellation without blocking on an admitted pull."""
        self._cancel.set()

    def close(self) -> None:
        """Stop future pulls, keeping completed truth and caller inputs intact."""
        if self._pulling:
            # The optional adapter waits for its admitted pull before closing.
            raise InputError("Wait for an active record pull before closing")
        if not self._closed:
            self._closed = True
            self._iterator = None
            if not self._finished:
                self._issues.exhausted(
                    "Record stream closed before its tail was checked",
                    location="jsonld.records",
                )

    def _stop(self, message: str) -> None:
        """Retain an explicit unchecked tail after cancellation or a stream cap."""
        self._issues.exhausted(message, location="jsonld.records")
        self._closed = True
        self._iterator = None

    def _record(self, frame: object, index: int) -> JSONLDRecordResult:
        """Prepare/check only the actual current frame and its independent scope."""
        operation = Budget(self._record_limits)
        issues = IssueCollector(operation)
        children: list[Report] = []
        preparation: JSONLDPreparation | None = None
        base = document_iri = None
        anchor = f"record[{index}]"
        try:
            if not isinstance(frame, JSONLDRecord):
                raise InputError("A record iterator must yield JSONLDRecord frames")
            for name, value in (
                ("base", frame.base),
                ("document_iri", frame.document_iri),
            ):
                if value is not None:
                    bound_text(value, operation)
                    absolute_iri(value)
                if name == "base":
                    base = value
                else:
                    document_iri = value
            payload = frame.payload
            if len(payload) > operation.limits.max_bytes:
                raise LimitExceeded("JSON-LD record exceeds max_bytes")
            operation.consume("work", len(payload) + 1)
            size = (
                len(payload.encode("utf-8"))
                if isinstance(payload, str)
                else len(payload)
            )
            if size > operation.limits.max_bytes:
                raise LimitExceeded("JSON-LD record exceeds max_bytes")
            operation.consume("bytes", size)
            document = decode_json(
                payload,
                source=document_iri or anchor,
                max_bytes=operation.limits.max_bytes,
                max_depth=operation.limits.max_depth,
                budget=operation,
            )
            if self._cancel.is_set():
                raise LimitExceeded("Record stream cancelled before preparation")
            processor()
            candidate = prepare_jsonld(
                document,
                base=base,
                document_iri=document_iri,
                document_loader=self._resources.for_budget(operation),
                budget=operation,
            )
            try:
                retain_report(candidate.report, operation, issues)
            except LimitExceeded:
                issues.retain_completed(candidate.report)
                raise
            preparation = candidate
            children.append(preparation.report)
            if (
                preparation.source is not None
                and preparation.report.verified
                and self._check is not None
            ):
                if self._cancel.is_set():
                    raise LimitExceeded(
                        "Record stream cancelled before its required check"
                    )
                checked = self._check(preparation.source, operation)
                if not isinstance(checked, Report):
                    raise InputError("A record check must return a Report")
                try:
                    retain_report(checked, operation, issues)
                except LimitExceeded:
                    # Returned callback metadata is not trusted merely because
                    # its finding objects were paid by this record operation.
                    issues.retain_completed(checked)
                    raise
                children.append(checked)
        except (InputError, UnicodeError) as error:
            failed = _failure(
                error
                if isinstance(error, InputError)
                else InputError("Record text requires valid Unicode"),
                operation,
                anchor,
            )
            children.append(failed)
            if preparation is None:
                preparation = JSONLDPreparation(None, failed)
            for child in children:
                issues.retain_completed(child)
            try:
                retain_report(failed, operation, issues)
            except LimitExceeded:
                issues.exhausted(
                    "Record report adoption exceeded selected limits", location=anchor
                )
        report = Report(
            "jsonld.record",
            1,
            tuple(issues),
            requested=1,
            complete=bool(children) and all(child.complete for child in children),
            children=tuple(children),
            regime="independent-jsonld-document",
        )
        assert preparation is not None
        return JSONLDRecordResult(index, base, document_iri, preparation, report)

    def __next__(self) -> JSONLDRecordResult:
        """Perform one admitted pull; failed final syntax cannot erase prior yields."""
        if self._closed or self._finished:
            raise StopIteration
        if self._pulling:
            raise InputError("Record streams support only one admitted pull")
        if self._cancel is not None and self._cancel.is_set():
            self._stop("Record stream cancelled before the next pull")
            raise StopIteration
        self._pulling = True
        try:
            self._operation.consume("work")
            # Precharge the result slot before actual preparation/callback work.
            self._operation.consume("states")
            assert self._iterator is not None
            try:
                frame = next(self._iterator)
            except StopIteration:
                if not self._processed:
                    self._issues.append(
                        Issue(
                            "jsonld.stream_empty",
                            "No independent JSON-LD records were supplied",
                            kind="unverified",
                        )
                    )
                self._finished = True
                self._iterator = None
                raise
            if self._processed >= self._max_records:
                self._stop(
                    "Record stream exceeds max_records; another frame remains unchecked"
                )
                raise StopIteration
            result = self._record(frame, self._processed)
            self._current = result
            self._current_adopted = False
            self._processed += 1
            try:
                retain_report(result.report, self._operation, self._issues)
                self._current_adopted = True
            except LimitExceeded:
                # Raw current truth is inspectable even when summary adoption
                # exhausts; only paid aggregate findings enter the summary.
                self._issues.retain_completed(result.report)
                self._stop("Record stream exceeded selected limits")
            if result.report.outcome == "exhausted":
                self._stop(
                    "Record preparation exhausted; remaining frames were not read"
                )
            if self._cancel is not None and self._cancel.is_set():
                self._stop("Record stream cancelled after its admitted pull")
            return result
        except LimitExceeded as stopped:
            self._stop("Record stream exceeded selected limits")
            raise StopIteration from stopped
        except StopIteration:
            raise
        except BaseException:
            # A caller may catch a trusted producer/check failure. Resuming must
            # never skip that required frame and later manufacture completion.
            self._stop("Record stream interrupted before required completion")
            raise
        finally:
            self._pulling = False


def check_jsonld_records(
    records: Iterable[JSONLDRecord],
    *,
    document_loader: DocumentLoader | None = None,
    record_limits: Limits | None = None,
    limits: Limits | None = None,
    max_records: int = 1000,
    check: RecordCheck | None = None,
    cancel: threading.Event | None = None,
) -> JSONLDRecordStream:
    """Create a synchronous pull stream; no input record is read at construction."""
    return JSONLDRecordStream(
        records,
        document_loader=document_loader,
        record_limits=record_limits,
        limits=limits,
        max_records=max_records,
        check=check,
        cancel=cancel,
    )
