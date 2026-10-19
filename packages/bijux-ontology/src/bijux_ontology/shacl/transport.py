"""Bounded, cancellable transport to the isolated SHACL process."""

import subprocess
import sys
import threading
import time
from contextlib import suppress
from typing import BinaryIO

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget
from bijux_ontology.processes.ownership import spawn_owned_process, stop_owned_process

_stop_group = stop_owned_process


def execute_backend(payload: bytes, *, cap: int, timeout: float | None) -> bytes:
    """Drain both streams while writing input, and reap every started process."""
    try:
        process = spawn_owned_process(
            [sys.executable, "-I", "-B", "-m", "bijux_ontology.shacl.worker", str(cap)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ResourceError("SHACL worker could not be started") from error
    overflow = threading.Event()
    stdout = bytearray()
    stderr = bytearray()

    def read(stream: BinaryIO, target: bytearray) -> None:
        """Drain one pipe while retaining at most its permitted byte count."""
        try:
            while chunk := stream.read(8192):
                remaining = cap - len(target)
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    break
        finally:
            stream.close()

    def write(stream: BinaryIO) -> None:
        """Feed the request concurrently so a full output pipe cannot deadlock."""
        try:
            stream.write(payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            stream.close()

    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    threads = [
        threading.Thread(target=write, args=(process.stdin,), daemon=True),
        threading.Thread(target=read, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read, args=(process.stderr, stderr), daemon=True),
    ]
    started = time.monotonic()
    failure: BaseException | None = None
    try:
        for thread in threads:
            thread.start()
        while process.poll() is None:
            if overflow.is_set():
                raise LimitExceeded("SHACL process output exceeds protocol byte bound")
            if timeout is not None and time.monotonic() - started >= timeout:
                raise LimitExceeded(
                    "SHACL process exceeded requested wall-clock deadline"
                )
            try:
                process.wait(timeout=0.02)
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            _stop_group(process, failure=failure)
        finally:
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
    if overflow.is_set():
        raise LimitExceeded("SHACL process output exceeds protocol byte bound")
    if process.returncode and not stdout:
        detail = stderr.decode(errors="replace")[:2000]
        raise ResourceError(f"SHACL worker exited {process.returncode}: {detail}")
    return bytes(stdout)


class SessionBackend:
    """One SHACL-only fixed-plan worker with bounded request/response framing."""

    def __init__(self, preparation: bytes, *, cap: int) -> None:
        self._preparation = preparation
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
                    "SHACL worker pipes remain held after bounded cleanup"
                )
            elif process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError) as error:
                            cleanup_error = cleanup_error or ResourceError(
                                f"Cannot close SHACL worker pipe: {error}"
                            )
            if (
                self._stderr_thread is not None
                and self._stderr_thread.ident is not None
            ):
                self._stderr_thread.join(timeout=0.2)
                if self._stderr_thread.is_alive():
                    self._closed.set()
                    cleanup_error = cleanup_error or ResourceError(
                        "SHACL diagnostics pipe remains held after bounded cleanup"
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
            raise ResourceError("SHACL session is closed")
        if cancel is not None and cancel.is_set():
            raise LimitExceeded("SHACL request was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise LimitExceeded("SHACL process exceeded requested wall-clock deadline")

    def _start(self) -> None:
        try:
            process = spawn_owned_process(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "bijux_ontology.shacl.worker",
                    str(self._cap),
                    "--session",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise ResourceError("SHACL worker could not be started") from error
        with self._guard:
            if self._closed.is_set():
                _stop_group(process)
                raise ResourceError("SHACL session is closed")
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
            raise LimitExceeded("SHACL request exceeds protocol byte bound")
        if budget is not None:
            if not isinstance(budget, Budget):
                raise InputError("SHACL transport requires a Budget")
            budget.consume("work", len(payload) + 1)
            budget.consume("states", 16)
            budget.consume("bytes", len(payload) + 2048)
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise ResourceError("SHACL session worker is not available")
        incoming, outgoing = process.stdout, process.stdin
        complete = threading.Event()
        output: list[bytes] = []
        errors: list[Exception] = []

        def read_exact(amount: int) -> bytes:
            data = bytearray()
            while len(data) < amount:
                chunk = incoming.read(min(8192, amount - len(data)))
                if not chunk:
                    raise ResourceError("SHACL worker closed its response pipe")
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
                    raise LimitExceeded("SHACL response exceeds protocol byte bound")
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
                        "SHACL process diagnostics exceed protocol byte bound"
                    )
                if process.poll() is not None:
                    raise ResourceError(f"SHACL worker exited {process.returncode}")
            self._interrupted(deadline, cancel)
            if self._stderr_overflow.is_set():
                raise LimitExceeded(
                    "SHACL process diagnostics exceed protocol byte bound"
                )
            if errors:
                if isinstance(errors[0], LimitExceeded):
                    raise errors[0]
                raise ResourceError(
                    f"SHACL worker protocol failed: {errors[0]}"
                ) from errors[0]
            return output[0]
        except BaseException as error:
            self.reset(failure=error)
            raise
        finally:
            thread.join(timeout=0.2)

    def execute(
        self,
        payload: bytes,
        *,
        deadline: float | None,
        cancel: threading.Event | None,
        budget: Budget | None = None,
    ) -> bytes:
        """Replay a fixed plan on fresh starts and exchange one independent job."""
        from bijux_ontology._json import decode_json

        self._interrupted(deadline, cancel)
        try:
            if self._process is None or self._process.poll() is not None:
                self.reset()
                self._start()
                prepared = self._exchange(
                    self._preparation, deadline, cancel, budget=budget
                )
                if budget is not None:
                    budget.consume("work", len(prepared) + 1)
                    budget.consume("states", 2 * len(prepared) + 8)
                    budget.consume("bytes", 64 * len(prepared) + 512)
                response = decode_json(prepared, max_bytes=self._cap)
                if not isinstance(response, dict):
                    raise ResourceError("Invalid SHACL session preparation response")
                status = response.get("status")
                if status == "ready":
                    if set(response) != {"status"}:
                        raise ResourceError(
                            "Invalid SHACL session preparation envelope"
                        )
                elif isinstance(status, str) and status in {
                    "exception",
                    "exhausted",
                    "unavailable",
                    "failure",
                    "input_error",
                    "shapes_invalid",
                }:
                    self.reset()
                    return prepared
                else:
                    raise ResourceError("Expected SHACL preparation acknowledgement")
            return self._exchange(payload, deadline, cancel, budget=budget)
        except BaseException as error:
            self.reset(failure=error)
            raise
