"""Bounded owned worker lifetime and process-group cancellation."""

import contextlib
import os
import subprocess
import time
from threading import Event, Thread

from bijux_ontology.errors import ResourceError
from bijux_ontology.processes import stop_owned_process as stop_process

__all__ = ["run_worker", "stop_process"]


def run_worker(
    command: list[str], *, timeout: float, cap: int, cancellation: Event | None
) -> tuple[int | None, str, str]:
    """Drain bounded diagnostics and interrupt deadlines, cancellation or overflow."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
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
    started = time.monotonic()
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
            if time.monotonic() - started >= timeout:
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
    detail = output.decode("utf-8", errors="replace")
    if cleanup_error is not None:
        if not stopped:
            raise cleanup_error
        detail = f"{cleanup_error}; {detail}"[:cap]
    return process.returncode, stopped, detail
