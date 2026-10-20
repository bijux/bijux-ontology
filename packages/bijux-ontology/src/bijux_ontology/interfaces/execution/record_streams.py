"""One admitted asynchronous pull over the ordinary independent-document stream."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Self

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.jsonld.streams.checking import JSONLDRecordStream
from bijux_ontology.jsonld.streams.types import JSONLDRecordResult


def _pull(stream: JSONLDRecordStream) -> JSONLDRecordResult | None:
    """Keep StopIteration inside the thread, without prefetching another frame."""
    return next(stream, None)


class AsyncJSONLDRecords:
    """Bridge the same stream, callbacks and resources with one private thread.

    Use an async context manager. Concurrent pulls are rejected rather than
    queued. Cancellation signals the synchronous stream and awaits its admitted
    call before closing; repeated cancellation cannot abandon the call. Producer,
    loader, callback and PyLD execution remain cooperative: an unresponsive
    caller function can delay cleanup. No hard preemption or timeout is claimed.
    Caller-owned producers and the event loop's default executor stay untouched.
    """

    def __init__(self, stream: JSONLDRecordStream) -> None:
        """Retain one explicit stream without reading a frame or creating a thread."""
        if not isinstance(stream, JSONLDRecordStream):
            raise InputError("Async records require a JSONLDRecordStream")
        self._stream = stream
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._active: asyncio.Future[JSONLDRecordResult | None] | None = None
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def report(self) -> Report:
        """Expose the synchronous stream's actual completed and unchecked truth."""
        return self._stream.report

    @property
    def last_result(self) -> JSONLDRecordResult | None:
        """Retain the sole completed record even when cancellation prevents delivery."""
        return self._stream.last_result

    async def __aenter__(self) -> Self:
        """Bind exactly one event loop and private bounded admission lifetime."""
        if self._loop is not None:
            raise InputError("An async record stream cannot be entered twice")
        self._stream.__enter__()
        self._loop = asyncio.get_running_loop()
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="bijux-record"
        )
        return self

    async def __aexit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> None:
        """Drain the admitted pull before propagating a caller body failure."""
        await self.aclose()

    def __aiter__(self) -> "AsyncJSONLDRecords":
        """Return this single-pass adapter without consuming another record."""
        return self

    def _require_loop(self) -> None:
        """Keep admission and cleanup on the context's owning event loop."""
        if self._loop is None or self._loop is not asyncio.get_running_loop():
            raise InputError("Async records require their open owning context")

    async def __anext__(self) -> JSONLDRecordResult:
        """Await one shielded pull, retaining admission through cancellation cleanup."""
        self._require_loop()
        if self._closed or self._closing:
            raise StopAsyncIteration
        if self._active is not None:
            raise InputError("Async record streams admit only one pull")
        assert self._loop is not None and self._pool is not None
        future = self._loop.run_in_executor(self._pool, _pull, self._stream)
        self._active = future
        interrupted = False
        result = None
        failure: Exception | None = None
        try:
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError:
                    interrupted = True
                    self._stream.cancel()
                    if future.done():
                        break
                except Exception as error:  # noqa: BLE001 - drain before rethrowing caller failure
                    failure = error
                    break
        finally:
            self._active = None
        if interrupted or failure is not None:
            await self.aclose()
        if interrupted:
            raise asyncio.CancelledError from failure
        if failure is not None:
            raise failure
        if result is None:
            raise StopAsyncIteration
        return result

    async def _finish_close(self) -> None:
        """Wait for the same admitted call before closing or joining idle threads."""
        self._closing = True
        if self._active is not None:
            self._stream.cancel()
            # The active consumer retains its callback error. Cleanup waits for
            # the same future and owns only the stream and idle thread lifetime.
            await asyncio.gather(self._active, return_exceptions=True)
        self._stream.close()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
        self._closed = True

    async def aclose(self) -> None:
        """Resist repeated caller cancellation until the admitted pull has settled."""
        self._require_loop()
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._finish_close())
        interrupted = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                interrupted = True
                self._stream.cancel()
        if interrupted:
            raise asyncio.CancelledError
