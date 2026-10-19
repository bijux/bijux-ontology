"""Bounded owned-process transport for local RDF engine workers."""

import math
import os
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Literal

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.processes import stop_owned_process

_reap = stop_owned_process


def checkpoint(deadline: float, cancel: threading.Event | None) -> None:
    """Bound caller preparation and worker computation by the same deadline."""
    if cancel is not None and cancel.is_set():
        raise LimitExceeded("RDF operation was cancelled")
    if time.monotonic() >= deadline:
        raise LimitExceeded("RDF operation exceeded its wall-clock deadline")


WorkerModule = Literal[
    "bijux_ontology.rdf.queries.worker",
    "bijux_ontology.rdf.canonical.worker",
    "bijux_ontology.rdf.patches.correspondence",
]


def execute_worker(
    module: WorkerModule,
    payload: bytes,
    *,
    cap: int,
    timeout: float,
    cancel: threading.Event | None = None,
) -> bytes:
    """Run an owned RDF worker and reap its process group.

    Payloads and replies are opaque bytes. Domain modules own their protocols;
    this transport owns bounded pipes, cancellation and the worker lifetime.
    """
    if module not in (
        "bijux_ontology.rdf.queries.worker",
        "bijux_ontology.rdf.canonical.worker",
        "bijux_ontology.rdf.patches.correspondence",
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
    failure: BaseException | None = None
    cleanup_error: ResourceError | None = None
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
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            _reap(process, failure=failure)
        except ResourceError as error:
            if failure is not None:
                raise
            cleanup_error = error
        finally:
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
    try:
        checkpoint(deadline, cancel)
        if overflow.is_set():
            raise LimitExceeded("Local RDF output exceeds protocol byte bound")
        if process.returncode:
            detail = diagnostics.decode(errors="replace")[:2000]
            raise ResourceError(
                f"Local RDF worker exited {process.returncode}: {detail}"
            )
    except InputError as error:
        if cleanup_error is not None:
            error.args = (f"{error}; {cleanup_error}",)
        raise
    if cleanup_error is not None:
        raise cleanup_error
    return bytes(output)
