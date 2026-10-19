"""Opaque bounded frames and deterministic ownership for reusable local workers."""

import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from io import BufferedReader
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.execution.processes.ownership import (
    spawn_owned_process,
    stop_owned_process,
)


@dataclass(frozen=True, slots=True, eq=False)
class WorkerGeneration:
    """Identity-owned observation of one actual process launched by a session."""

    number: int
    pid: int


@dataclass(eq=False)
class _GenerationState:
    """Keep detached cleanup attached to its original process and threads."""

    token: WorkerGeneration
    process: subprocess.Popen[bytes]
    stderr_overflow: threading.Event = field(default_factory=threading.Event)
    stderr_thread: threading.Thread | None = None
    exchange_thread: threading.Thread | None = None
    cleaned: threading.Event = field(default_factory=threading.Event)
    cleanup_error: ResourceError | None = None


class FramedSession:
    """One explicit owned process with bounded opaque request/response frames."""

    def __init__(
        self, command: tuple[str, ...], *, cap: int, label: str = "Worker"
    ) -> None:
        if type(cap) is not int or cap < 1:
            raise InputError("Framed process byte bound must be a positive integer")
        if (
            type(command) is not tuple
            or not command
            or any(type(v) is not str for v in command)
        ):
            raise InputError("Framed process command requires an immutable text tuple")
        self._command = command
        self._label = label
        self._cap = cap
        self._worker: _GenerationState | None = None
        self._retiring: _GenerationState | None = None
        self._guard = threading.RLock()
        self._closed = threading.Event()
        self._launches = 0

    @property
    def launches(self) -> int:
        """Count actual process launches, including recovery after failed jobs."""
        return self._launches

    @property
    def cap(self) -> int:
        """Expose the fixed opaque frame bound to protocol owners."""
        return self._cap

    @property
    def generation(self) -> WorkerGeneration | None:
        """Observe a live owned process without exposing its mutable handles."""
        with self._guard:
            worker = self._worker
            return (
                worker.token
                if worker is not None and worker.process.poll() is None
                else None
            )

    def close(self) -> None:
        """Cancel any active request and permanently release this session."""
        self._closed.set()
        self.reset()

    def reset(
        self,
        *,
        generation: WorkerGeneration | None = None,
        failure: BaseException | None = None,
    ) -> None:
        """Discard only the selected generation and reap it outside the state lock."""
        with self._guard:
            worker = self._worker
            if worker is not None and (
                generation is None or worker.token is generation
            ):
                self._worker = None
                self._retiring = worker
                owner = True
            else:
                worker = self._retiring
                if worker is None or (
                    generation is not None and worker.token is not generation
                ):
                    return
                owner = False
        if owner:
            try:
                self._cleanup(worker, failure)
            finally:
                with self._guard:
                    if self._retiring is worker:
                        self._retiring = None
                    worker.cleaned.set()
        elif not worker.cleaned.wait(1.5):
            self._closed.set()
            error = ResourceError(
                f"{self._label} generation cleanup did not finish within its bound"
            )
            if failure is None:
                raise error
            failure.add_note(str(error))
        elif worker.cleanup_error is not None and failure is None:
            raise worker.cleanup_error

    def _cleanup(self, worker: _GenerationState, failure: BaseException | None) -> None:
        """Release exactly one detached generation's process and pipe owners."""
        process = worker.process
        cleanup_error: ResourceError | None = None
        try:
            stop_owned_process(process, failure=failure)
        except ResourceError as error:
            cleanup_error = error
        finally:
            exchange = worker.exchange_thread
            if exchange is not None and exchange.ident is not None:
                exchange.join(timeout=0.2)
            if exchange is not None and exchange.is_alive():
                self._closed.set()
                cleanup_error = cleanup_error or ResourceError(
                    f"{self._label} worker pipes remain held after bounded cleanup"
                )
            else:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError) as error:
                            cleanup_error = cleanup_error or ResourceError(
                                f"Cannot close {self._label} worker pipe: {error}"
                            )
            if (
                worker.stderr_thread is not None
                and worker.stderr_thread.ident is not None
            ):
                worker.stderr_thread.join(timeout=0.2)
                if worker.stderr_thread.is_alive():
                    self._closed.set()
                    cleanup_error = cleanup_error or ResourceError(
                        f"{self._label} diagnostics pipe remains held "
                        "after bounded cleanup"
                    )
            elif process.stderr is not None:
                try:
                    process.stderr.close()
                except (OSError, ValueError) as error:
                    cleanup_error = cleanup_error or ResourceError(
                        f"Cannot close {self._label} diagnostics pipe: {error}"
                    )
        worker.cleanup_error = cleanup_error
        if cleanup_error is not None:
            if failure is not None:
                if isinstance(failure, InputError):
                    failure.args = (f"{failure}; {cleanup_error}",)
                else:
                    failure.add_note(str(cleanup_error))
            else:
                raise cleanup_error

    def checkpoint(
        self, deadline: float | None, cancel: threading.Event | None
    ) -> None:
        """Check this session's close state and the current request's stop policy."""
        if self._closed.is_set():
            raise ResourceError(f"{self._label} session is closed")
        if cancel is not None and cancel.is_set():
            raise LimitExceeded(f"{self._label} request was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise LimitExceeded(
                f"{self._label} process exceeded requested wall-clock deadline"
            )

    def ensure_generation(
        self, deadline: float | None, cancel: threading.Event | None
    ) -> WorkerGeneration:
        """Reuse one live worker or start after its retired predecessor is reaped."""
        worker = None
        try:
            while True:
                self.checkpoint(deadline, cancel)
                with self._guard:
                    self.checkpoint(deadline, cancel)
                    worker, retiring = self._worker, self._retiring
                    if worker is not None and worker.process.poll() is None:
                        if worker.stderr_overflow.is_set():
                            raise LimitExceeded(
                                f"{self._label} process diagnostics exceed "
                                "protocol byte bound"
                            )
                        return worker.token
                    if worker is None and retiring is None:
                        worker = self._start_locked()
                        self._start_diagnostics_locked(worker)
                        break
                if worker is not None:
                    self.reset(generation=worker.token)
                elif retiring is not None:
                    while not retiring.cleaned.wait(0.01):
                        self.checkpoint(deadline, cancel)
            self.checkpoint(deadline, cancel)
        except BaseException as error:
            failure = (
                ResourceError(f"{self._label} worker diagnostics could not be started")
                if isinstance(error, RuntimeError)
                else error
            )
            if worker is not None:
                self.reset(generation=worker.token, failure=failure)
            if failure is not error:
                raise failure from error
            raise
        return worker.token

    def _start_locked(self) -> _GenerationState:
        """Install one process and its diagnostic owner under the short state lock."""
        try:
            process = spawn_owned_process(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise ResourceError(f"{self._label} worker could not be started") from error
        self._launches += 1
        worker = _GenerationState(
            WorkerGeneration(self._launches, process.pid), process
        )
        self._worker = worker
        return worker

    def _start_diagnostics_locked(self, worker: _GenerationState) -> None:
        """Register diagnostics after the caller owns failed-start cleanup."""
        process = worker.process

        def drain() -> None:
            """Drain diagnostics without retaining repeated backend output."""
            assert process.stderr is not None
            size = 0
            try:
                # A reusable worker need not close diagnostics after a frame.
                # Read available bytes without waiting for a full buffered block.
                stream = cast(BufferedReader, process.stderr)
                while chunk := stream.read1(min(8192, self._cap - size + 1)):
                    size += len(chunk)
                    if size > self._cap:
                        worker.stderr_overflow.set()
                        break
            except (OSError, ValueError):
                return
            finally:
                process.stderr.close()

        worker.stderr_thread = threading.Thread(target=drain, daemon=True)
        worker.stderr_thread.start()

    def _require_generation(
        self,
        generation: WorkerGeneration,
        deadline: float | None,
        cancel: threading.Event | None,
    ) -> _GenerationState:
        """Reject foreign or retired identities before exchange and publication."""
        with self._guard:
            self.checkpoint(deadline, cancel)
            worker = self._worker
            if worker is None or worker.token is not generation:
                raise ResourceError(
                    f"{self._label} worker generation is no longer active"
                )
            return worker

    def exchange(
        self,
        generation: WorkerGeneration,
        payload: bytes,
        deadline: float | None,
        cancel: threading.Event | None,
        *,
        budget: Budget | None = None,
    ) -> bytes:
        """Exchange one bounded frame with the active owned worker generation."""
        import struct

        if len(payload) > self._cap:
            raise LimitExceeded(f"{self._label} request exceeds protocol byte bound")
        if budget is not None:
            if not isinstance(budget, Budget):
                raise InputError(f"{self._label} transport requires a Budget")
            budget.consume("work", len(payload) + 1)
            budget.consume("states", 16)
            budget.consume("bytes", len(payload) + 2048)
        worker = self._require_generation(generation, deadline, cancel)
        process = worker.process
        if process.stdin is None or process.stdout is None:
            raise ResourceError(f"{self._label} session worker is not available")
        incoming, outgoing = process.stdout, process.stdin
        complete = threading.Event()
        output: list[bytes] = []
        errors: list[Exception] = []

        def read_exact(amount: int) -> bytes:
            """Read the admitted frame length or reject a closed response pipe."""
            data = bytearray()
            while len(data) < amount:
                chunk = incoming.read(min(8192, amount - len(data)))
                if not chunk:
                    raise ResourceError(
                        f"{self._label} worker closed its response pipe"
                    )
                data.extend(chunk)
            return bytes(data)

        def communicate() -> None:
            """Bound frames before allocation and signal all pipe failures."""
            try:
                outgoing.write(struct.pack(">Q", len(payload)))
                outgoing.write(payload)
                outgoing.flush()
                size = struct.unpack(">Q", read_exact(8))[0]
                if size > self._cap:
                    raise LimitExceeded(
                        f"{self._label} response exceeds protocol byte bound"
                    )
                if budget is not None:
                    budget.consume("work", size + 1)
                    budget.consume("states", 4 + (size + 8191) // 8192)
                    budget.consume("bytes", 3 * size + 128)
                output.append(read_exact(size))
            except Exception as error:
                errors.append(error)
            finally:
                complete.set()
                if self._worker is not worker:
                    for stream in (incoming, outgoing):
                        with suppress(OSError, ValueError):
                            stream.close()

        thread = threading.Thread(target=communicate, daemon=True)
        registered = False
        try:
            with self._guard:
                self._require_generation(generation, deadline, cancel)
                if worker.stderr_overflow.is_set():
                    raise LimitExceeded(
                        f"{self._label} process diagnostics exceed protocol byte bound"
                    )
                if (
                    worker.exchange_thread is not None
                    and worker.exchange_thread.is_alive()
                ):
                    raise ResourceError(
                        f"{self._label} generation already has an active frame"
                    )
                worker.exchange_thread = thread
                registered = True
                try:
                    thread.start()
                except RuntimeError as error:
                    raise ResourceError(
                        f"{self._label} frame could not be started"
                    ) from error
            while not complete.wait(0.01):
                self.checkpoint(deadline, cancel)
                if worker.stderr_overflow.is_set():
                    raise LimitExceeded(
                        f"{self._label} process diagnostics exceed protocol byte bound"
                    )
                if process.poll() is not None:
                    raise ResourceError(
                        f"{self._label} worker exited {process.returncode}"
                    )
            self.checkpoint(deadline, cancel)
            if worker.stderr_overflow.is_set():
                raise LimitExceeded(
                    f"{self._label} process diagnostics exceed protocol byte bound"
                )
            if errors:
                if isinstance(errors[0], LimitExceeded):
                    raise errors[0]
                raise ResourceError(
                    f"{self._label} worker protocol failed: {errors[0]}"
                ) from errors[0]
            with self._guard:
                self._require_generation(generation, deadline, cancel)
                return output[0]
        except BaseException as error:
            if registered or worker.stderr_overflow.is_set():
                self.reset(generation=generation, failure=error)
            raise
        finally:
            if thread.ident is not None:
                thread.join(timeout=0.2)
