"""Bounded owned worker lifetime and process-group cancellation."""

import contextlib
import os
import signal
import subprocess
import time
from threading import Event, Thread


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Reap an owned process and its POSIX descendants, including nested validators."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def run_worker(
    command: list[str], *, timeout: float, cap: int, cancellation: Event | None
) -> tuple[int, str, str]:
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
    finally:
        stop_process(process)
        reader.join(timeout=1)
        if process.stdout is not None and not reader.is_alive():
            process.stdout.close()
    if overflow.is_set():
        stopped = stopped or "execution.limit_exceeded"
    return process.returncode, stopped, output.decode("utf-8", errors="replace")
