"""One sealed official ROBOT release and bounded owned native CLI execution."""

import hashlib
import os
import time
from pathlib import Path

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.execution.processes.transport import run_worker
from bijux_ontology.rdf.stores.seals import StoreFile

from .types import RobotBackend, RobotRun

ROBOT_VERSION = "1.9.11"
ROBOT_SHA256 = "53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26"


def prepare_robot_backend(
    java: Path,
    jar: Path,
    *,
    sha256: str,
    artifacts: Path,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> RobotBackend:
    """Seal the explicit official standalone release; never install or discover it."""
    operation = Operation(limits, budget, 30, None)
    if any(not isinstance(path, Path) for path in (java, jar, artifacts)):
        raise InputError("ROBOT requires explicit Java, jar and artifact Paths")
    for path in (java, jar, artifacts):
        bound_text(str(path), operation)
    if type(sha256) is not str:
        raise InputError("The ROBOT pin requires plain text")
    bound_text(sha256, operation)
    if sha256 != ROBOT_SHA256:
        raise ResourceError("Unsupported ROBOT standalone release identity")
    if not java.is_absolute() or not jar.is_absolute():
        raise InputError("ROBOT runtime paths must be absolute")
    if jar.is_symlink() or not jar.is_file():
        raise ResourceError("The explicit ROBOT jar is unavailable")
    before = jar.stat()
    operation.consume("bytes", before.st_size + 1)
    operation.consume("work", before.st_size // 65536 + 2)
    digest = hashlib.sha256()
    remaining = before.st_size
    with jar.open("rb") as stream:
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ResourceError("The ROBOT jar changed during capture")
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            raise ResourceError("The ROBOT jar grew during capture")
    after = jar.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ) or digest.hexdigest() != sha256:
        raise ResourceError("The ROBOT jar differs from its supplied release pin")
    return RobotBackend._owned(
        java=java,
        jar=jar,
        seal=StoreFile(
            jar.name, after.st_size, after.st_mtime_ns, after.st_ino, sha256
        ),
        artifacts=artifacts.absolute(),
    )


def _verify(backend: RobotBackend, operation: Operation) -> None:
    """Reject changed sealed files and ambient JVM option injection."""
    if type(backend) is not RobotBackend:
        raise InputError("ROBOT requires an explicitly prepared runtime")
    if any(
        name in os.environ
        for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")
    ):
        raise ResourceError("Ambient JVM options are outside the ROBOT profile")
    for path in (backend.java, backend.jar, backend.artifacts):
        bound_text(str(path), operation)
    operation.consume("work")
    if (
        not backend.java.is_file()
        or backend.jar.is_symlink()
        or not backend.jar.is_file()
    ):
        raise ResourceError("The explicit ROBOT runtime is unavailable")
    actual = backend.jar.stat()
    if (actual.st_size, actual.st_mtime_ns, actual.st_ino) != (
        backend.seal.size,
        backend.seal.modified_ns,
        backend.seal.inode,
    ):
        raise ResourceError("The sealed ROBOT jar changed")


def run_robot(
    backend: RobotBackend,
    arguments: tuple[str, ...],
    watched: tuple[Path, ...],
    operation: Operation,
    heap_mb: int,
) -> RobotRun:
    """Own native lifetime and bounded file adoption for domain-built commands.

    Domain callers stage owned inputs and select their fixed command dialect.
    Opaque JVM work is bounded by heap, deadline, diagnostic pipes and monitored
    output files; it has no host instruction counter or hard file-size claim.
    """
    if type(heap_mb) is not int or not 32 <= heap_mb <= 4096:
        raise InputError("ROBOT heap requires an integer from 32 to 4096 MiB")
    _verify(backend, operation)
    if type(arguments) is not tuple or type(watched) is not tuple:
        raise InputError("Native ROBOT commands require owned finite tuples")
    if not arguments or not 1 <= len(watched) <= 2:
        raise InputError("Native ROBOT requires arguments and one or two outputs")
    operation.consume("states", len(arguments) + len(watched))
    for argument in arguments:
        bound_text(argument, operation)
    for path in watched:
        if not isinstance(path, Path):
            raise InputError("Native ROBOT outputs require owned Paths")
        bound_text(str(path), operation)
    cap = min(
        operation.limits.max_bytes,
        operation.remaining("bytes") // 16,
        operation.remaining("work") // 8,
    )
    if cap < 1024:
        raise LimitExceeded("No bounded ROBOT output allowance remains")
    operation.consume("bytes", 16 * cap)
    operation.consume("work", 6 * cap)
    operation.consume("states", 32)
    command = [str(backend.java), f"-Xmx{heap_mb}m", "-jar", str(backend.jar)]
    operation.consume("work", 0)
    status, stop, identity = run_worker(
        command + ["--version"],
        timeout=operation.deadline - time.monotonic(),
        cap=1024,
        cancellation=operation.cancel,
    )
    if stop:
        raise LimitExceeded(stop)
    if status != 0 or identity.strip() != "ROBOT version " + ROBOT_VERSION:
        raise ResourceError("The native ROBOT identity is unavailable or unsupported")
    command.extend(arguments)
    for argument in command:
        bound_text(argument, operation)

    def guard() -> float:
        """Monitor only caller-owned files during native process supervision."""
        for path in watched:
            if path.exists() and (path.is_symlink() or path.stat().st_size > cap):
                raise LimitExceeded("Native ROBOT output exceeds its file cap")
        return operation.deadline

    operation.consume("work", 0)
    status, stop, diagnostic = run_worker(
        command,
        timeout=operation.deadline - time.monotonic(),
        cap=cap,
        cancellation=operation.cancel,
        deadline=guard,
    )
    if stop:
        raise LimitExceeded(stop)
    guard()
    _verify(backend, operation)
    outputs: list[bytes | None] = []
    for path in watched:
        if not path.is_file():
            outputs.append(None)
            continue
        if path.is_symlink():
            raise ResourceError("Native ROBOT output ownership changed")
        with path.open("rb") as reader:
            content = reader.read(cap + 1)
        if len(content) > cap:
            raise LimitExceeded("Native ROBOT output exceeds its file cap")
        outputs.append(content)
    return RobotRun(identity.strip(), status, diagnostic, tuple(outputs))
