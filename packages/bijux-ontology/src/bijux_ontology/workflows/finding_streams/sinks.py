"""Explicit synchronous JSONL and callback sinks without hidden queues."""

from collections.abc import Callable
from typing import Protocol

from bijux_ontology.errors import InputError

from .types import StreamEvent


class _BinaryOutput(Protocol):
    """The two actual binary-output operations required by this owned sink."""

    def write(self, payload: bytes, /) -> int:
        """Return the exact accepted byte count or raise."""
        ...

    def flush(self) -> None:
        """Report any failure finishing the accepted output."""
        ...


class JsonlFindingSink:
    """Write to an explicitly caller-owned binary stream; never close its handle."""

    def __init__(self, stream: _BinaryOutput) -> None:
        """Retain a caller destination without creating directories or files."""
        if not callable(getattr(stream, "write", None)) or not callable(
            getattr(stream, "flush", None)
        ):
            raise InputError("A JSONL sink requires a writable binary stream")
        self._stream = stream

    def write(self, event: StreamEvent, payload: bytes) -> None:
        """Reject short or text-mode writes rather than silently truncating a frame."""
        written = self._stream.write(payload)
        if type(written) is not int or written != len(payload):
            raise OSError("Finding JSONL sink did not accept the complete frame")

    def finish(self) -> None:
        """Flush output without assuming durability beyond the caller's stream."""
        self._stream.flush()


class CallbackFindingSink:
    """Apply synchronous backpressure with no thread, prefetch or callback isolation.

    Callbacks may block and cannot be hard-preempted. Cancellation/deadlines are
    checked around each call. Caller retention and callback side effects remain
    caller-owned; this workflow does not infer validation from callback success.
    """

    def __init__(self, callback: Callable[[StreamEvent], None]) -> None:
        """Accept an output consumer, never an executable validation callback."""
        if not callable(callback):
            raise InputError("A callback sink requires a callable")
        self._callback = callback

    def write(self, event: StreamEvent, payload: bytes) -> None:
        """Wait for this exact frame's callback before admitting another record."""
        self._callback(event)

    def finish(self) -> None:
        """No hidden queue or executor remains to drain."""
