"""Bound asynchronous admission while the ordinary worker owns native execution."""

import asyncio
import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.interfaces.execution.executor import ExecutionResult, WorkerExecutor

from .types import AsyncBatchResult, PreparedWorkerCheck

Completion = tuple[ExecutionResult, int]


def _incomplete(message: str, *, cancelled: bool = False) -> ExecutionResult:
    """Keep admission and cancellation loss explicit, without semantic success."""
    return ExecutionResult(
        Report(
            "execution.async",
            issues=(
                Issue(
                    "execution.cancelled" if cancelled else "execution.limit_exceeded",
                    message,
                    kind="exhausted",
                ),
            ),
            requested=1,
            complete=False,
        )
    )


def _consume_cancellation(baseline: int) -> None:
    """Consume only cancellation requests handled by this report-oriented call."""
    task = asyncio.current_task()
    if task is not None:
        while task.cancelling() > baseline:
            task.uncancel()


def _cancelled(result: ExecutionResult) -> ExecutionResult:
    """Preserve a completed race result alongside the required cancelled scope."""
    if any(issue.code == "execution.cancelled" for issue in result.report.issues):
        return result
    return ExecutionResult(
        combine_reports(
            "execution.async",
            result.report,
            _incomplete("Async request cancelled", cancelled=True).report,
        ),
        result.data,
    )


class AsyncChecks:
    """Own a bounded bridge to an explicit synchronous WorkerExecutor.

    Use as an async context manager. Running plus queued requests and their
    prepared bytes have fixed caps; overflow returns an incomplete result instead
    of growing an executor queue. Native work always runs in the existing owned
    process path. No default executor, Python callback dispatch or global pool is
    used. Synchronous request preparation remains an explicit caller operation.

    Task cancellation is converted into an actual incomplete ExecutionResult
    after signalling that request's process and awaiting cleanup. Independent
    requests have independent events. Closing signals all admitted requests,
    drains the queue without starting new workers and awaits every bridge call.
    """

    def __init__(
        self,
        executor: WorkerExecutor,
        *,
        max_running: int = 2,
        max_queued: int = 8,
    ) -> None:
        """Validate finite host admission before allocating threads or tasks."""
        if type(executor) is not WorkerExecutor:
            raise InputError("Async checks require an explicit WorkerExecutor")
        if type(max_running) is not int or type(max_queued) is not int:
            raise InputError("Async capacity requires integer bounds")
        if (
            not 0 < max_running <= executor.limits.max_resources
            or max_queued < 0
            or max_running + max_queued > executor.limits.max_states
        ):
            raise InputError("Async capacity exceeds the selected execution bounds")
        self._executor = executor
        self._max_running = max_running
        self._max_queued = max_queued
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._slots: asyncio.Semaphore | None = None
        self._drained: asyncio.Event | None = None
        self._active: dict[asyncio.Future[Completion], Event] = {}
        self._admitted = 0
        self._request_bytes = 0
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def executor(self) -> WorkerExecutor:
        """Retain the exact immutable installed execution policy for this session."""
        return self._executor

    @property
    def max_running(self) -> int:
        """Expose the fixed bound used by both admission and the bridge pool."""
        return self._max_running

    @property
    def max_queued(self) -> int:
        """Expose the fixed bound on admitted requests waiting for a running slot."""
        return self._max_queued

    async def __aenter__(self) -> "AsyncChecks":
        """Bind one event loop and one private, lazily started fixed thread pool."""
        if self._loop is not None:
            raise InputError("An async check session cannot be entered twice")
        self._loop = asyncio.get_running_loop()
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_running, thread_name_prefix="bijux-check"
        )
        self._slots = asyncio.Semaphore(self.max_running)
        self._drained = asyncio.Event()
        self._drained.set()
        return self

    async def __aexit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> None:
        """Finish all owned cleanup before propagating the caller's body error."""
        await self.aclose()

    def prepare(
        self, operation: str, parameters: dict[str, object]
    ) -> PreparedWorkerCheck:
        """Synchronously own request data under this executor's explicit limits."""
        return PreparedWorkerCheck(operation, parameters, limits=self.executor.limits)

    def _require_loop(self) -> None:
        """Keep session lifecycle and all admission state on its owning loop."""
        if self._loop is not asyncio.get_running_loop() or self._closed:
            raise InputError("Async checks require their open owning context")

    def _invoke(self, request: PreparedWorkerCheck, event: Event) -> Completion:
        """Decode and perform actual bounded worker work off the event loop."""
        result = self.executor.run(
            request.operation, request.parameters, cancellation=event
        )
        size = 0
        if result.data is not None:
            encoded = json.dumps(
                result.data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            size = len(encoded.encode("utf-8"))
        return result, size

    async def _settle(
        self, future: asyncio.Future[Completion], event: Event, baseline: int
    ) -> Completion:
        """Signal cancellation and keep awaiting the original owned bridge call."""
        cancelled = False
        while True:
            try:
                result, size = await asyncio.shield(future)
                return (_cancelled(result) if cancelled else result), size
            except asyncio.CancelledError:
                cancelled = True
                event.set()
                _consume_cancellation(baseline)
                if future.cancelled():
                    return _incomplete("Async bridge cancelled", cancelled=True), 0

    async def _run(self, request: PreparedWorkerCheck) -> Completion:
        """Admit before submission; release the slot only after native cleanup."""
        if self._loop is not asyncio.get_running_loop():
            raise InputError("Async checks require their owning event loop")
        if type(request) is not PreparedWorkerCheck:
            raise InputError("Async execution requires a PreparedWorkerCheck")
        if self._closing:
            return _incomplete("Async session is closing", cancelled=True), 0
        if (
            self._admitted >= self.max_running + self.max_queued
            or self._request_bytes + request.request_bytes
            > self.executor.limits.max_total_bytes
            or request.request_bytes > self.executor.limits.max_bytes
        ):
            return _incomplete("Async admission capacity is full"), 0
        assert self._slots is not None and self._drained is not None
        assert self._loop is not None and self._pool is not None
        self._admitted += 1
        self._request_bytes += request.request_bytes
        self._drained.clear()
        acquired = False
        future = None
        task = asyncio.current_task()
        baseline = task.cancelling() if task is not None else 0
        try:
            try:
                await self._slots.acquire()
            except asyncio.CancelledError:
                _consume_cancellation(baseline)
                return _incomplete("Queued request cancelled", cancelled=True), 0
            acquired = True
            if self._close_task is not None:
                return _incomplete(
                    "Async session closed before startup", cancelled=True
                ), 0
            event = Event()
            future = self._loop.run_in_executor(
                self._pool, self._invoke, request, event
            )
            self._active[future] = event
            return await self._settle(future, event, baseline)
        finally:
            if future is not None:
                self._active.pop(future, None)
            if acquired:
                self._slots.release()
            self._admitted -= 1
            self._request_bytes -= request.request_bytes
            if not self._admitted:
                self._drained.set()

    async def run(self, request: PreparedWorkerCheck) -> ExecutionResult:
        """Return the actual synchronous result, or its explicit incomplete tail."""
        self._require_loop()
        result, _ = await self._run(request)
        return result

    async def _finish_close(self) -> None:
        """Drain all admission exits before joining already idle bridge threads."""
        self._closing = True
        for event in self._active.values():
            event.set()
        assert self._drained is not None and self._pool is not None
        await self._drained.wait()
        # Every bridge future has completed, including process cleanup. Joining
        # the idle pool cannot wait for active native or request-processing work.
        self._pool.shutdown(wait=True)
        self._closed = True

    async def aclose(self) -> None:
        """Resist caller cancellation until every owned worker has been reaped."""
        if self._loop is None:
            return
        if self._loop is not asyncio.get_running_loop():
            raise InputError("An async session closes on its owning event loop")
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish_close())
        task = asyncio.current_task()
        baseline = task.cancelling() if task is not None else 0
        while True:
            try:
                await asyncio.shield(self._close_task)
                return
            except asyncio.CancelledError:
                _consume_cancellation(baseline)

    async def batch(self, requests: Sequence[PreparedWorkerCheck]) -> AsyncBatchResult:
        """Check finite input with a bounded window and retain completed prefixes.

        Each worker uses its ordinary per-request limits. A separate cumulative
        host Budget bounds input capture and result/data collection. Exhaustion
        stops new submissions and drains the finite active window; raw reports
        from that tail remain inspectable, while unqualified data is omitted.
        Cancellation affects only this batch's child requests and is converted
        to an incomplete aggregate after their owned cleanup has finished.
        """
        self._require_loop()
        operation = Budget(self.executor.limits)
        issues = IssueCollector(operation)
        results: list[ExecutionResult | None] = []
        tasks: dict[asyncio.Task[Completion], int] = {}
        requested = 1
        selected: list[PreparedWorkerCheck] = []
        next_index = 0
        stopped = False
        cancelled = False
        current = asyncio.current_task()
        baseline = current.cancelling() if current is not None else 0
        try:
            if isinstance(cast(object, requests), str | bytes) or not isinstance(
                requests, Sequence
            ):
                raise InputError("An async batch requires a finite request sequence")
            count = len(requests)
            requested = max(1, count)
            operation.consume("states", 2 * count)
            operation.consume("bytes", 128 + 32 * count)
            results = [None] * count
            for index in range(count):
                operation.consume("work")
                try:
                    request = requests[index]
                except IndexError as error:
                    raise InputError("Async batch changed during capture") from error
                if type(request) is not PreparedWorkerCheck:
                    raise InputError("Batch inputs require PreparedWorkerCheck")
                bound_text(request.operation, operation)
                bound_text(request._parameters_json, operation)
                selected.append(request)
            while next_index < count or tasks:
                try:
                    if self._closing:
                        stopped = cancelled = True
                        for task in tasks:
                            task.cancel()
                    while (
                        not stopped
                        and next_index < count
                        and len(tasks) < self.max_running
                    ):
                        operation.consume("states")
                        task = asyncio.create_task(self._run(selected[next_index]))
                        tasks[task] = next_index
                        next_index += 1
                    if not tasks:
                        break
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        index = tasks.pop(task)
                        if task.cancelled():
                            result, size = (
                                _incomplete("Batch request cancelled", cancelled=True),
                                0,
                            )
                        else:
                            result, size = task.result()
                        results[index] = ExecutionResult(result.report)
                        if not stopped:
                            operation.consume("bytes", size)
                            retain_report(result.report, operation, issues)
                            results[index] = result
                except asyncio.CancelledError:
                    _consume_cancellation(baseline)
                    stopped = cancelled = True
                    for task in tasks:
                        task.cancel()
                except LimitExceeded as error:
                    issues.exhausted(str(error), location="execution.async.batch")
                    stopped = True
                    for task in tasks:
                        task.cancel()
        except LimitExceeded as error:
            issues.exhausted(str(error), location="execution.async.batch")
        except InputError:
            try:
                issues.append(
                    Issue(
                        "input.invalid",
                        "Async batch input is invalid",
                        kind="input_error",
                    )
                )
            except LimitExceeded as error:
                issues.exhausted(str(error), location="execution.async.batch")
        finally:
            if tasks:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            _consume_cancellation(baseline)
                        except Exception:
                            break
        if cancelled:
            issues.exhausted("Async batch cancelled", location="execution.async.batch")
        reports = tuple(result.report for result in results if result is not None)
        return AsyncBatchResult(
            tuple(results),
            Report(
                "execution.async.batch",
                len(reports),
                tuple(issues),
                requested=requested,
                children=reports,
                complete=bool(selected)
                and len(reports) == len(selected)
                and not stopped,
                regime="owned-worker-per-request-with-bounded-collection",
            ),
        )
