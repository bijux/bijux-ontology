"""One bounded pull cursor with maintained Arrow ownership and explicit tail truth."""

from threading import Event
from typing import Self

from rdflib import Dataset, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.sources.loading import prepare_rdf

from .checking import check_row, convert_row, schema_indices
from .preparation import backend
from .types import ArrowBatchResult, ArrowContract, ArrowRowResult


class ArrowRecordStream:
    """Consume one native cursor cooperatively, with no hidden prefetch or worker.

    Direct RecordBatchReader inputs transfer cursor consumption/closure to this
    adapter. Tables and protocol producers use maintained C-stream ownership.
    Producer callbacks and native imports are in-process cooperative work;
    no hard preemption or zero-copy RDF construction is promised.
    """

    def __init__(
        self,
        source: object,
        contract: ArrowContract,
        *,
        emit_rdf: bool,
        max_batches: int,
        max_rows: int,
        max_batch_rows: int,
        limits: Limits | None,
        budget: Budget | None,
        timeout: float,
        cancel: Event | None,
    ) -> None:
        """Retain one actual reader and capture its native schema before pulls."""
        if type(contract) is not ArrowContract:
            raise InputError("Use a prepared ArrowContract")
        if limits is None and budget is None:
            limits = contract.models.limits
        self._operation = Operation(limits, budget, timeout, cancel)
        if self._operation.limits != contract.models.limits:
            raise InputError("Arrow operation limits differ from the compiled model")
        if type(emit_rdf) is not bool or any(
            type(value) is not int or value <= 0
            for value in (max_batches, max_rows, max_batch_rows)
        ):
            raise InputError("Arrow bounds require positive integers and an emit flag")
        self._operation.consume("states", 16)
        self._operation.consume("bytes", 2048)
        self._native = backend()
        self._contract = contract
        self._emit = emit_rdf
        self._max_batches, self._max_rows, self._max_batch_rows = (
            max_batches,
            max_rows,
            max_batch_rows,
        )
        self._index = self._rows = 0
        self._issues = IssueCollector(self._operation)
        self._reports: list[Report] = []
        self._subjects: set[URIRef] = set()
        self._closed = self._ended = False
        self._active = False
        self.last_result: ArrowBatchResult | None = None
        native = self._native
        if isinstance(source, native.RecordBatchReader):
            reader = source
        elif isinstance(source, native.RecordBatch):
            reader = native.RecordBatchReader.from_batches(source.schema, (source,))
        else:
            try:
                reader = native.RecordBatchReader.from_stream(source)
            except (TypeError, native.ArrowException) as error:
                raise ResourceError(
                    "The supplied Arrow C stream could not be imported"
                ) from error
        self._reader = reader
        try:
            self._schema = reader.schema
            self._indices = schema_indices(
                native, self._schema, contract, self._operation
            )
        except BaseException:
            reader.close()
            raise
        self._report = self._aggregate()

    def _aggregate(self) -> Report:
        """Publish reserved references and exact unchecked-tail status."""
        return Report(
            "arrow.records",
            self._rows,
            tuple(self._issues),
            requested=self._rows + int(not self._ended),
            complete=self._ended and all(child.complete for child in self._reports),
            children=tuple(self._reports),
            regime="explicit-arrow-column-bindings",
        )

    @property
    def report(self) -> Report:
        """Expose the last owned aggregate, including any unfinished stream tail."""
        return self._report

    @property
    def closed(self) -> bool:
        """Expose actual validation reader closure without touching caller buffers."""
        return self._closed

    def __enter__(self) -> Self:
        """Use this single cursor until end, explicit close or consumer failure."""
        if self._closed:
            raise InputError("The Arrow record stream is closed")
        return self

    def __exit__(self, kind: object, error: object, traceback: object) -> None:
        """Release the real reader on all consumer context exits."""
        self.close()

    def __iter__(self) -> Self:
        """Return the same cursor without pulling or caching another batch."""
        return self

    def _stop(self, error: Exception, children: list[Report]) -> Report:
        """Retain real paid children before recording the incomplete remaining work."""
        for child in children:
            self._issues.retain_completed(child)
        if isinstance(error, LimitExceeded):
            self._issues.exhausted(str(error), location=f"arrow/batch/{self._index}")
        else:
            try:
                self._issues.append(
                    Issue(
                        "arrow.stream_unverified"
                        if isinstance(error, ResourceError | OSError)
                        else "arrow.input_error",
                        str(error),
                        f"arrow/batch/{self._index}",
                        kind="unverified"
                        if isinstance(error, ResourceError | OSError)
                        else "input_error",
                    )
                )
            except LimitExceeded as exhausted:
                self._issues.exhausted(str(exhausted), location="arrow")
        self._reader.close()
        self._closed = True
        return Report(
            "arrow.batch",
            len(children),
            tuple(self._issues),
            requested=len(children) + 1,
            complete=False,
            children=tuple(children),
        )

    def __next__(self) -> ArrowBatchResult:
        """Admit one batch, prepay scalar copies and retain completed row findings."""
        if self._closed:
            raise StopIteration
        if self._active:
            raise InputError("Concurrent/reentrant Arrow pulls are unsupported")
        self._active = True
        rows: list[ArrowRowResult] = []
        children: list[Report] = []
        output = None
        try:
            operation = self._operation
            operation.consume("work", len(self._reports) + 1)
            operation.consume("states", len(self._reports) + 2)
            operation.consume("bytes", 512 + 32 * len(self._reports))
            if self._index >= self._max_batches:
                raise LimitExceeded("max_batches leaves an unchecked Arrow tail")
            try:
                batch = self._reader.read_next_batch()
            except StopIteration:
                self._ended = self._closed = True
                self._reader.close()
                self._report = self._aggregate()
                raise
            except Exception as error:
                raise ResourceError(
                    "The actual Arrow producer failed: " + type(error).__name__
                ) from error
            count, width = batch.num_rows, batch.num_columns
            operation.consume("states", 8 + width)
            operation.consume("work", 8 + width)
            if count > self._max_batch_rows or self._rows + count > self._max_rows:
                raise LimitExceeded("Arrow batch/row bound leaves unchecked records")
            if not batch.schema.equals(self._schema, check_metadata=True):
                raise ResourceError("Actual Arrow batch schema changed")
            # Source buffers already belong to the producer. Prepay all owned
            # scalar, lexical and RDF allocations before decoding any value.
            size = batch.get_total_buffer_size()
            if size > operation.limits.max_bytes:
                raise LimitExceeded("Native Arrow batch buffers exceed max_bytes")
            operation.consume("bytes", 24 * size + 2048 + 1024 * count * (width + 1))
            operation.consume("states", 16 + 8 * count * (width + 1))
            operation.consume("work", size + count * (width + 1))
            dataset = Dataset()
            converted = []
            for row in range(count):
                operation.consume("work")
                subject, cells, raw = convert_row(
                    self._native,
                    batch,
                    row,
                    self._index,
                    self._indices,
                    self._contract,
                    dataset,
                    operation,
                    self._issues,
                )
                if subject in self._subjects:
                    raise ResourceError(
                        "Repeated Arrow subjects make row-specific contracts ambiguous"
                    )
                operation.consume("states")
                operation.consume("bytes", 64)
                self._subjects.add(subject)
                converted.append((subject, cells, raw))
                children.append(raw)
                retain_report(raw, operation, self._issues)
            source = prepare_rdf(dataset, budget=operation)
            for row, (subject, cells, raw) in enumerate(converted):
                checked = check_row(
                    source,
                    subject,
                    row,
                    self._index,
                    cells,
                    raw,
                    self._contract,
                    operation,
                )
                rows.append(checked)
                children.append(checked.report)
                self._rows += 1
                retain_report(checked.report, operation, self._issues)
            if self._emit:
                output = source
            report = Report(
                "arrow.batch",
                len(rows),
                requested=count,
                children=tuple(children),
                complete=all(child.complete for child in children),
            )
        except StopIteration:
            raise
        except (InputError, OSError) as error:
            report = self._stop(error, children)
        finally:
            self._active = False
        result = ArrowBatchResult(self._index, tuple(rows), report, output)
        self.last_result = result
        self._reports.append(report)
        self._index += 1
        self._report = self._aggregate()
        return result

    def close(self) -> None:
        """Release cursors; early closure leaves the required tail unknown."""
        if self._closed:
            return
        if self._active:
            raise InputError("Close an Arrow stream only after its admitted pull")
        self._reader.close()
        self._closed = True
        try:
            self._issues.append(
                Issue(
                    "arrow.remaining_unverified",
                    "Validation closed before observing native end of stream",
                    "arrow",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            self._issues.exhausted(str(error), location="arrow")
        self._report = self._aggregate()


def open_arrow_records(
    source: object,
    contract: ArrowContract,
    *,
    emit_rdf: bool = False,
    max_batches: int = 1000,
    max_rows: int = 100000,
    max_batch_rows: int = 10000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ArrowRecordStream:
    """Open the explicit optional Arrow interface without materializing a JSON list."""
    return ArrowRecordStream(
        source,
        contract,
        emit_rdf=emit_rdf,
        max_batches=max_batches,
        max_rows=max_rows,
        max_batch_rows=max_batch_rows,
        limits=limits,
        budget=budget,
        timeout=timeout,
        cancel=cancel,
    )
