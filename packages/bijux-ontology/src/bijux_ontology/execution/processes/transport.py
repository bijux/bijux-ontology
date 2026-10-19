"""Bounded owned worker lifetime and process-group cancellation."""

import contextlib
import subprocess
import sys
import time
from collections.abc import Callable
from threading import Event, Thread
from typing import BinaryIO

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.processes.ownership import spawn_owned_process
from bijux_ontology.execution.processes.ownership import (
    stop_owned_process as stop_process,
)

__all__ = ["checkpoint", "exchange_worker", "run_worker", "stop_process"]


def _validate_worker(
    command: list[str],
    cap: int,
    timeout: float | None,
    cancel: Event | None,
    *,
    allow_no_deadline: bool = False,
) -> None:
    """Qualify supervision policy before creating an owned process or pipe."""
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(value, str) or "\x00" in value for value in command)
    ):
        raise InputError("Worker commands require a nonempty argument list")
    if type(cap) is not int or cap <= 0:
        raise InputError("Workers require a positive output cap")
    if not (timeout is None and allow_no_deadline) and (
        timeout is None
        or type(timeout) not in (int, float)
        or not 0 < timeout <= sys.float_info.max
    ):
        raise InputError("Worker timeout must be finite and positive")
    if cancel is not None and not isinstance(cancel, Event):
        raise InputError("Worker cancellation requires an Event")


def _deadline_reached(probe: Callable[[], float | None] | None) -> bool:
    """Accept only finite current host deadlines from a trusted bounded probe."""
    if probe is None:
        return False
    if not callable(probe):
        raise InputError("Worker deadline requires a callable probe")
    value = probe()
    if value is None:
        return False
    if (
        type(value) not in (int, float)
        or not -sys.float_info.max <= value <= sys.float_info.max
    ):
        raise InputError("Worker deadline probe must return a finite time or None")
    return time.monotonic() >= value


def run_worker(
    command: list[str],
    *,
    timeout: float,
    cap: int,
    cancellation: Event | None,
    deadline: Callable[[], float | None] | None = None,
) -> tuple[int | None, str, str]:
    """Drain bounded diagnostics and interrupt deadlines, cancellation or overflow.

    An optional trusted host probe supplies the current inner-operation deadline;
    it must perform only bounded local work so supervision remains responsive.
    """
    _validate_worker(command, cap, timeout, cancellation)
    if cancellation is not None and cancellation.is_set():
        return None, "execution.cancelled", ""
    if _deadline_reached(deadline):
        return None, "execution.deadline", ""
    started = time.monotonic()
    try:
        process = spawn_owned_process(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as error:
        raise ResourceError("Worker could not be started") from error
    output = bytearray()
    overflow = Event()

    def drain() -> None:
        """Read continuously without allowing diagnostic output to fill memory."""
        assert process.stdout is not None
        while chunk := process.stdout.read(8192):
            remaining = cap - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                return

    reader = Thread(target=drain, daemon=True)
    reader.start()
    stopped = ""
    failure: BaseException | None = None
    cleanup_error: ResourceError | None = None
    try:
        while process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                stopped = "execution.cancelled"
                break
            if overflow.is_set():
                stopped = "execution.limit_exceeded"
                break
            if time.monotonic() - started >= timeout or _deadline_reached(deadline):
                stopped = "execution.deadline"
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.02)
    except KeyboardInterrupt:
        stopped = "execution.cancelled"
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            stop_process(process, failure=failure)
        except ResourceError as error:
            cleanup_error = error
        finally:
            reader.join(timeout=1)
            if process.stdout is not None and not reader.is_alive():
                process.stdout.close()
    if overflow.is_set():
        stopped = stopped or "execution.limit_exceeded"
    if not stopped:
        if cancellation is not None and cancellation.is_set():
            stopped = "execution.cancelled"
        elif time.monotonic() - started >= timeout or _deadline_reached(deadline):
            stopped = "execution.deadline"
    detail = output.decode("utf-8", errors="replace")
    if cleanup_error is not None:
        if not stopped:
            raise cleanup_error
        detail = f"{cleanup_error}; {detail}"[:cap]
    return process.returncode, stopped, detail


def checkpoint(deadline: float | None, cancel: Event | None) -> None:
    """Bound caller preparation and worker computation by the same deadline."""
    if cancel is not None and cancel.is_set():
        raise LimitExceeded("Worker operation was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise LimitExceeded("Worker operation exceeded its wall-clock deadline")


def exchange_worker(
    command: list[str],
    payload: bytes,
    *,
    cap: int,
    timeout: float | None,
    cancel: Event | None = None,
    completed: Callable[[bytes], None] | None = None,
) -> bytes:
    """Exchange opaque bounded bytes with one explicitly owned no-shell command.

    Protocols and runtime identity belong to callers. This transport owns bounded
    concurrent pipes, an optional explicit deadline and descendant cleanup.
    An explicit terminal handoff receives complete successful bytes before a
    later stop; its domain still qualifies and retains that bounded receipt.
    """
    _validate_worker(command, cap, timeout, cancel, allow_no_deadline=True)
    if not isinstance(payload, bytes):
        raise InputError("Workers require a bytes request")
    if len(payload) > cap:
        raise LimitExceeded("Worker input exceeds protocol byte bound")
    if completed is not None and not callable(completed):
        raise InputError("Completed worker receipts require a callable")
    deadline = None if timeout is None else time.monotonic() + timeout
    checkpoint(deadline, cancel)
    try:
        process = spawn_owned_process(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ResourceError("Worker could not be started") from error
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    output, diagnostics = bytearray(), bytearray()
    overflow = Event()
    input_failed = Event()
    delivered = Event()
    stdout_done, stderr_done = Event(), Event()

    def read(stream: BinaryIO, target: bytearray, done: Event) -> None:
        """Drain without buffering more than the declared protocol bound."""
        try:
            while chunk := stream.read(8192):
                remaining = cap - len(target)
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    break
        finally:
            stream.close()
        done.set()

    def write(stream: BinaryIO) -> None:
        """Write concurrently so a blocked input pipe cannot hide cancellation."""
        try:
            if stream.write(payload) != len(payload):
                input_failed.set()
            stream.flush()
        except OSError:
            input_failed.set()
        finally:
            try:
                stream.close()
            except OSError:
                input_failed.set()
        if not input_failed.is_set():
            delivered.set()

    threads = (
        Thread(target=write, args=(process.stdin,), daemon=True),
        Thread(target=read, args=(process.stdout, output, stdout_done), daemon=True),
        Thread(
            target=read, args=(process.stderr, diagnostics, stderr_done), daemon=True
        ),
    )
    failure: BaseException | None = None
    cleanup_error: ResourceError | None = None
    try:
        for thread in threads:
            thread.start()
        while process.poll() is None:
            checkpoint(deadline, cancel)
            if overflow.is_set():
                raise LimitExceeded("Worker output exceeds protocol byte bound")
            try:
                process.wait(timeout=0.01)
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            stop_process(process, failure=failure)
        except ResourceError as error:
            if failure is not None:
                raise
            cleanup_error = error
        finally:
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
    response: bytes | None = None
    try:
        if completed is None:
            checkpoint(deadline, cancel)
        if overflow.is_set():
            raise LimitExceeded("Worker output exceeds protocol byte bound")
        if process.returncode:
            detail = diagnostics.decode(errors="replace")[:2000]
            raise ResourceError(f"Worker exited {process.returncode}: {detail}")
        if input_failed.is_set():
            raise ResourceError("Worker closed its request pipe before delivery")
        if completed is not None:
            if cleanup_error is not None:
                raise cleanup_error
            if (
                not delivered.is_set()
                or not stdout_done.is_set()
                or not stderr_done.is_set()
            ):
                raise ResourceError("Worker did not finish its bounded byte exchange")
            response = bytes(output)
            completed(response)
            checkpoint(deadline, cancel)
    except InputError as error:
        if cleanup_error is not None and error is not cleanup_error:
            error.args = (f"{error}; {cleanup_error}",)
        raise
    if cleanup_error is not None:
        raise cleanup_error
    return response if response is not None else bytes(output)
