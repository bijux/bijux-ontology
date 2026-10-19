"""One sealed official ROBOT runtime and bounded owned native table execution."""

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.processes.transport import run_worker
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.stores.seals import StoreFile
from bijux_ontology.rdf.terms import bound_text

from .types import TemplateBackend

ROBOT_VERSION = "1.9.11"
ROBOT_SHA256 = "53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26"


def prepare_template_backend(
    java: Path,
    jar: Path,
    *,
    sha256: str,
    artifacts: Path,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> TemplateBackend:
    """Seal the explicit official standalone release; never install or discover it."""
    operation = Operation(limits, budget, 30, None)
    if any(not isinstance(path, Path) for path in (java, jar, artifacts)):
        raise InputError("Templates require explicit Java, jar and artifact Paths")
    for path in (java, jar, artifacts):
        bound_text(str(path), operation)
    if type(sha256) is not str:
        raise InputError("The ROBOT pin requires plain text")
    bound_text(sha256, operation)
    if sha256 != ROBOT_SHA256:
        raise ResourceError("Unsupported ROBOT standalone release identity")
    if not java.is_absolute() or not jar.is_absolute():
        raise InputError("Template runtime paths must be absolute")
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
    return TemplateBackend._owned(
        java=java,
        jar=jar,
        seal=StoreFile(
            jar.name, after.st_size, after.st_mtime_ns, after.st_ino, sha256
        ),
        artifacts=artifacts.absolute(),
    )


def _verify(backend: TemplateBackend, operation: Operation) -> None:
    """Reject changed sealed files and ambient JVM option injection."""
    if type(backend) is not TemplateBackend:
        raise InputError("Templates require an explicitly prepared runtime")
    if any(
        name in os.environ
        for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")
    ):
        raise ResourceError("Ambient JVM options are outside the template profile")
    for path in (backend.java, backend.jar, backend.artifacts):
        bound_text(str(path), operation)
    operation.consume("work")
    if (
        not backend.java.is_file()
        or backend.jar.is_symlink()
        or not backend.jar.is_file()
    ):
        raise ResourceError("The explicit template runtime is unavailable")
    actual = backend.jar.stat()
    if (actual.st_size, actual.st_mtime_ns, actual.st_ino) != (
        backend.seal.size,
        backend.seal.modified_ns,
        backend.seal.inode,
    ):
        raise ResourceError("The sealed ROBOT jar changed")


@dataclass(frozen=True)
class NativeTable:
    """Actual maintained CLI receipts, without interpreting stdout as an ontology."""

    identity: str
    returncode: int | None
    diagnostic: str
    output: bytes | None
    errors: bytes | None


def execute_table(
    backend: TemplateBackend,
    table: bytes,
    bindings: bytes,
    suffix: str,
    prefixes: tuple[tuple[str, str], ...],
    ontology_iri: str,
    operation: Operation,
    heap_mb: int,
) -> NativeTable:
    """Own heap/deadline/pipes and monitor actual output/error-file growth.

    Opaque JVM computation has no instruction counter. File growth is monitored
    while it runs and checked again after exit; oversized output is never read
    or accepted. Existing process-group ownership reaps cancellation and failure.
    """
    if type(heap_mb) is not int or not 32 <= heap_mb <= 4096:
        raise InputError("ROBOT heap requires an integer from 32 to 4096 MiB")
    _verify(backend, operation)
    cap = min(
        operation.limits.max_bytes,
        operation.remaining("bytes") // 16,
        operation.remaining("work") // 8,
    )
    if cap < 1024:
        raise LimitExceeded("No bounded template output allowance remains")
    operation.consume("bytes", 16 * cap + len(table) + len(bindings))
    operation.consume("work", 6 * cap)
    operation.consume("states", 32)
    backend.artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="table-template-", dir=backend.artifacts) as job:
        directory = Path(job)
        input_table, input_bindings, output, errors = (
            directory / name
            for name in (
                "table." + suffix,
                "bindings.ofn",
                "ontology.ofn",
                "errors.csv",
            )
        )
        input_table.write_bytes(table)
        input_bindings.write_bytes(bindings)
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
            raise ResourceError(
                "The native ROBOT identity is unavailable or unsupported"
            )
        command += [
            "template",
            "--input",
            str(input_bindings),
            "--ontology-iri",
            ontology_iri,
            "--template",
            str(input_table),
            "--errors",
            str(errors),
            "--output",
            str(output),
        ]
        for prefix, namespace in prefixes:
            command += ["--prefix", prefix + ": " + namespace]
        for argument in command:
            bound_text(argument, operation)

        def guard() -> float:
            """Check only owned local file growth during native supervision."""
            for path in (output, errors):
                if path.exists() and (path.is_symlink() or path.stat().st_size > cap):
                    raise LimitExceeded("Native template output exceeds its file cap")
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

        def read_output(path: Path) -> bytes | None:
            """Keep the actual read bounded even if a file changes after supervision."""
            if not path.is_file():
                return None
            if path.is_symlink():
                raise ResourceError("Native template output ownership changed")
            with path.open("rb") as reader:
                content = reader.read(cap + 1)
            if len(content) > cap:
                raise LimitExceeded("Native template output exceeds its file cap")
            return content

        return NativeTable(
            identity.strip(),
            status,
            diagnostic,
            read_output(output),
            read_output(errors),
        )
