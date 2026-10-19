"""Bounded, cancellable transport to the isolated SHACL process."""

import subprocess
import sys
import threading
import time
from typing import BinaryIO

from bijux_ontology.errors import LimitExceeded, ResourceError
from bijux_ontology.limits import Budget
from bijux_ontology.processes.ownership import spawn_owned_process, stop_owned_process
from bijux_ontology.processes.sessions import FramedSession

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


class SessionBackend(FramedSession):
    """One SHACL-only fixed-plan worker with its exact preparation handshake."""

    def __init__(self, preparation: bytes, *, cap: int) -> None:
        self._preparation = preparation
        super().__init__(
            (
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology.shacl.worker",
                str(cap),
                "--session",
            ),
            cap=cap,
            label="SHACL",
        )

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
