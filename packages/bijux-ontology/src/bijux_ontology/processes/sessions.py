"""Opaque bounded frames and deterministic ownership for reusable local workers."""

import subprocess
import threading
import time
from contextlib import suppress

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget
from bijux_ontology.processes.ownership import spawn_owned_process, stop_owned_process

_stop_group = stop_owned_process


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
        self._process: subprocess.Popen[bytes] | None = None
        self._guard = threading.Lock()
        self._closed = threading.Event()
        self._launches = 0
        self._stderr_overflow = threading.Event()
        self._stderr_thread: threading.Thread | None = None
        self._exchange_thread: threading.Thread | None = None

    @property
    def launches(self) -> int:
        """Count actual process launches, including recovery after failed jobs."""
        return self._launches

    def close(self) -> None:
        """Cancel any active request and permanently release this session."""
        self._closed.set()
        self.reset()

    def reset(self, *, failure: BaseException | None = None) -> None:
        """Discard failed worker state; a later job may replay the owned plan."""
        with self._guard:
            process, self._process = self._process, None
        if process is None:
            return
        cleanup_error: ResourceError | None = None
        try:
            if process is not None:
                _stop_group(process, failure=failure)
        except ResourceError as error:
            cleanup_error = error
        finally:
            exchange = self._exchange_thread
            if exchange is not None and exchange.ident is not None:
                exchange.join(timeout=0.2)
            if exchange is not None and exchange.is_alive():
                # Buffered close waits on an active read lock. The exchange owns
                # its detached handles until that read returns; forbid reuse.
                self._closed.set()
                cleanup_error = cleanup_error or ResourceError(
                    f"{self._label} worker pipes remain held after bounded cleanup"
                )
            elif process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError) as error:
                            cleanup_error = cleanup_error or ResourceError(
                                f"Cannot close {self._label} worker pipe: {error}"
                            )
            if (
                self._stderr_thread is not None
                and self._stderr_thread.ident is not None
            ):
                self._stderr_thread.join(timeout=0.2)
                if self._stderr_thread.is_alive():
                    self._closed.set()
                    cleanup_error = cleanup_error or ResourceError(
                        f"{self._label} diagnostics pipe remains held "
                        "after bounded cleanup"
                    )
        if cleanup_error is not None:
            if failure is not None:
                if isinstance(failure, InputError):
                    failure.args = (f"{failure}; {cleanup_error}",)
                else:
                    failure.add_note(str(cleanup_error))
            else:
                raise cleanup_error

    def _interrupted(
        self, deadline: float | None, cancel: threading.Event | None
    ) -> None:
        if self._closed.is_set():
            raise ResourceError(f"{self._label} session is closed")
        if cancel is not None and cancel.is_set():
            raise LimitExceeded(f"{self._label} request was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise LimitExceeded(
                f"{self._label} process exceeded requested wall-clock deadline"
            )

    def _start(self) -> None:
        try:
            process = spawn_owned_process(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise ResourceError(f"{self._label} worker could not be started") from error
        with self._guard:
            if self._closed.is_set():
                _stop_group(process)
                raise ResourceError(f"{self._label} session is closed")
            self._process = process
            self._launches += 1
        self._stderr_overflow = threading.Event()

        def drain() -> None:
            """Drain diagnostics without retaining repeated backend output."""
            assert process.stderr is not None
            size = 0
            try:
                while chunk := process.stderr.read(8192):
                    size += len(chunk)
                    if size > self._cap:
                        self._stderr_overflow.set()
                        break
            except (OSError, ValueError):
                return
            finally:
                process.stderr.close()

        self._stderr_thread = threading.Thread(target=drain, daemon=True)
        self._stderr_thread.start()

    def _exchange(
        self,
        payload: bytes,
        deadline: float | None,
        cancel: threading.Event | None,
        *,
        budget: Budget | None = None,
    ) -> bytes:
        import struct

        if len(payload) > self._cap:
            raise LimitExceeded(f"{self._label} request exceeds protocol byte bound")
        if budget is not None:
            if not isinstance(budget, Budget):
                raise InputError(f"{self._label} transport requires a Budget")
            budget.consume("work", len(payload) + 1)
            budget.consume("states", 16)
            budget.consume("bytes", len(payload) + 2048)
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise ResourceError(f"{self._label} session worker is not available")
        incoming, outgoing = process.stdout, process.stdin
        complete = threading.Event()
        output: list[bytes] = []
        errors: list[Exception] = []

        def read_exact(amount: int) -> bytes:
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
                if self._process is not process:
                    for stream in (incoming, outgoing):
                        with suppress(OSError, ValueError):
                            stream.close()

        thread = threading.Thread(target=communicate, daemon=True)
        self._exchange_thread = thread
        thread.start()
        try:
            while not complete.wait(0.01):
                self._interrupted(deadline, cancel)
                if self._stderr_overflow.is_set():
                    raise LimitExceeded(
                        f"{self._label} process diagnostics exceed protocol byte bound"
                    )
                if process.poll() is not None:
                    raise ResourceError(
                        f"{self._label} worker exited {process.returncode}"
                    )
            self._interrupted(deadline, cancel)
            if self._stderr_overflow.is_set():
                raise LimitExceeded(
                    f"{self._label} process diagnostics exceed protocol byte bound"
                )
            if errors:
                if isinstance(errors[0], LimitExceeded):
                    raise errors[0]
                raise ResourceError(
                    f"{self._label} worker protocol failed: {errors[0]}"
                ) from errors[0]
            return output[0]
        except BaseException as error:
            self.reset(failure=error)
            raise
        finally:
            thread.join(timeout=0.2)
