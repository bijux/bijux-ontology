"""Bounded owned-process transport for local RDF engine workers."""

import math
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from typing import BinaryIO, Literal

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError


def checkpoint(deadline: float, cancel: threading.Event | None) -> None:
    """Bound caller preparation and worker computation by the same deadline."""
    if cancel is not None and cancel.is_set():
        raise LimitExceeded("RDF operation was cancelled")
    if time.monotonic() >= deadline:
        raise LimitExceeded("RDF operation exceeded its wall-clock deadline")


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Terminate the owned process group, including children of an exited leader."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


WorkerModule = Literal[
    "bijux_ontology.rdf.queries.worker", "bijux_ontology.rdf.canonical.worker"
]


def execute_worker(
    module: WorkerModule,
    payload: bytes,
    *,
    cap: int,
    timeout: float,
    cancel: threading.Event | None = None,
) -> bytes:
    """Run one of the two owned RDF workers and reap its process group.

    Payloads and replies are opaque bytes. Domain modules own their protocols;
    this transport owns bounded pipes, cancellation and the worker lifetime.
    """
    if module not in (
        "bijux_ontology.rdf.queries.worker",
        "bijux_ontology.rdf.canonical.worker",
    ):
        raise InputError("Unsupported RDF worker module")
    if not isinstance(payload, bytes) or type(cap) is not int or cap <= 0:
        raise InputError("RDF workers require bytes and a positive output cap")
    if len(payload) > cap:
        raise LimitExceeded("Local RDF input exceeds protocol byte bound")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise InputError("RDF worker timeout must be finite and positive")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("RDF cancellation requires a threading.Event")
    deadline = time.monotonic() + timeout
    checkpoint(deadline, cancel)
    if os.name != "posix":
        raise ResourceError("Local RDF process-group isolation is unavailable")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", module, str(cap)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ResourceError("Local RDF worker could not be started") from error
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    output, diagnostics = bytearray(), bytearray()
    overflow = threading.Event()

    def read(stream: BinaryIO, target: bytearray) -> None:
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

    def write(stream: BinaryIO) -> None:
        """Write concurrently so a blocked input pipe cannot hide cancellation."""
        try:
            stream.write(payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            stream.close()

    threads = (
        threading.Thread(target=write, args=(process.stdin,), daemon=True),
        threading.Thread(target=read, args=(process.stdout, output), daemon=True),
        threading.Thread(target=read, args=(process.stderr, diagnostics), daemon=True),
    )
    try:
        for thread in threads:
            thread.start()
        while process.poll() is None:
            checkpoint(deadline, cancel)
            if overflow.is_set():
                raise LimitExceeded("Local RDF output exceeds protocol byte bound")
            try:
                process.wait(timeout=0.01)
            except subprocess.TimeoutExpired:
                continue
    finally:
        _reap(process)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=1)
    checkpoint(deadline, cancel)
    if overflow.is_set():
        raise LimitExceeded("Local RDF output exceeds protocol byte bound")
    if process.returncode:
        detail = diagnostics.decode(errors="replace")[:2000]
        raise ResourceError(f"Local RDF worker exited {process.returncode}: {detail}")
    return bytes(output)
