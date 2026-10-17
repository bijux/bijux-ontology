"""Bounded, cancellable transport to the isolated SHACL process."""

import subprocess
import sys
import threading
import time
from typing import BinaryIO

from bijux_ontology.errors import LimitExceeded, ResourceError


def execute_backend(payload: bytes, *, cap: int, timeout: float | None) -> bytes:
    """Drain both streams while writing input, and reap every started process."""
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "bijux_ontology.shacl.worker", str(cap)],
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
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=1)
    if overflow.is_set():
        raise LimitExceeded("SHACL process output exceeds protocol byte bound")
    if process.returncode and not stdout:
        detail = stderr.decode(errors="replace")[:2000]
        raise ResourceError(f"SHACL worker exited {process.returncode}: {detail}")
    return bytes(stdout)
