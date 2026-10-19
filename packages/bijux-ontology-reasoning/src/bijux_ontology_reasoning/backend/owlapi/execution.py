"""Owned structured JVM invocation and pinned capability qualification."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any, cast

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.execution.processes.transport import run_worker
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.rdf.stores.seals import verify_files

from ..types import BackendCapability, OwlProfile, OwlProfileBackend
from .bundles import (
    LOADER_PROFILE,
    MAIN_CLASS,
    OWLAPI_VERSION,
    PROFILES,
    qualify_backend,
)
from .protocol import result_counter, result_text


def qualify_capability(
    data: dict[str, Any],
    backend: OwlProfileBackend,
    operation: Budget,
) -> BackendCapability:
    """Accept only the pinned actual runtime and four maintained checker identities."""
    if type(data.get("protocol")) is not int or data["protocol"] != 1:
        raise ResourceError("Unsupported OWL worker protocol")
    version = result_text(data.get("owlapi"), operation)
    java = result_text(data.get("java"), operation)
    vm = result_text(data.get("vm"), operation)
    pin = result_text(data.get("manifest_sha256"), operation)
    configuration = result_text(data.get("loader_profile"), operation)
    major = result_counter(data.get("java_major"), operation, 1000)
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != len(PROFILES):
        raise ResourceError("OWL worker omitted its profile capabilities")
    operation.consume("states", len(profiles))
    for value in profiles:
        result_text(value, operation)
    if (
        version != OWLAPI_VERSION
        or major < 11
        or pin != backend.manifest_sha256
        or tuple(profiles) != PROFILES
        or configuration != LOADER_PROFILE
    ):
        raise ResourceError(
            "OWL worker capability differs from the pinned supported bundle"
        )
    return BackendCapability(
        version,
        java,
        vm,
        backend.manifest_sha256,
        configuration,
        cast(tuple[OwlProfile, ...], PROFILES),
        Report("owl.backend", 1, requested=1, regime="owlapi-profile-capability"),
    )


def invoke_worker(
    backend: OwlProfileBackend,
    request: dict[str, Any],
    directory: Path,
    operation: Operation,
    heap_mb: int,
) -> dict[str, Any]:
    """Use shared owned transport; semantic results never come from process logs."""
    if type(heap_mb) is not int or not 32 <= heap_mb <= 4096:
        raise InputError("OWL heap_mb must be an integer between 32 and 4096")
    operation.consume("work")
    request.update(protocol=1, manifest_sha256=backend.manifest_sha256)
    operation.consume("bytes", json_bound(request, operation))
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    if len(payload) > operation.limits.max_bytes:
        raise LimitExceeded("OWL worker request exceeds max_bytes")
    input_path, output_path = directory / "request.json", directory / "result.json"
    with input_path.open("xb") as writer:
        writer.write(payload)
    classpath = []
    for name in backend.jars:
        bound_text(name, operation)
        path = str(backend.directory / name)
        bound_text(path, operation)
        operation.consume("states")
        classpath.append(path)
    size = sum(len(item.encode()) + 1 for item in classpath)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("OWL classpath exceeds max_bytes")
    operation.consume("bytes", size)
    command = [
        str(backend.java),
        f"-Xmx{heap_mb}m",
        "-Xss512k",
        "-XX:MaxDirectMemorySize=64m",
        "-XX:MaxMetaspaceSize=128m",
        "-XX:-HeapDumpOnOutOfMemoryError",
        "-XX:-UsePerfData",
        f"-XX:HeapDumpPath={directory / 'heap.hprof'}",
        f"-Djava.io.tmpdir={directory}",
        f"-XX:ErrorFile={directory / 'jvm-error.log'}",
        "-cp",
        os.pathsep.join(classpath),
        MAIN_CLASS,
        str(input_path),
        str(output_path),
        str(operation.limits.max_bytes),
    ]
    code, stopped, _diagnostics = run_worker(
        command,
        timeout=max(0.000001, operation.deadline - time.monotonic()),
        cap=operation.limits.max_bytes,
        cancellation=operation.cancel,
    )
    if stopped:
        raise LimitExceeded("OWL worker did not complete within owned execution limits")
    if code != 0:
        raise ResourceError("OWL worker process failed")
    verify_files(backend.directory, backend.files, operation)
    if output_path.is_symlink() or not output_path.is_file():
        raise ResourceError("OWL worker did not provide its owned structured result")
    if output_path.stat().st_size > operation.limits.max_bytes:
        raise LimitExceeded("OWL worker structured result exceeds max_bytes")
    with output_path.open("rb") as reader:
        raw = reader.read(operation.limits.max_bytes + 1)
    if len(raw) > operation.limits.max_bytes:
        raise LimitExceeded("OWL worker structured result exceeds max_bytes")
    operation.consume("bytes", len(raw))
    data = object_value(
        decode_json(
            raw,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        ),
        location="OWL worker result",
    )
    status = result_text(data.get("status"), operation)
    if status == "exhausted" and set(data) == {"protocol", "status"}:
        raise LimitExceeded("OWL worker exhausted its heap, stack or output bound")
    if status not in {"completed", "exhausted"}:
        raise ResourceError("OWL worker parsing or capability is unavailable")
    return data


def backend_capability(
    backend: OwlProfileBackend,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> BackendCapability:
    """Run an explicit bounded capability observation; imports start no JVM."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    try:
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="capability-", dir=backend.artifacts) as job:
            data = invoke_worker(
                backend, {"operation": "capability"}, Path(job), operation, heap_mb
            )
            return qualify_capability(data, backend, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.backend")
    except (InputError, OSError):
        try:
            issues.append(
                Issue(
                    "owl.backend_unavailable",
                    "The selected OWL backend is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.backend")
    return BackendCapability(
        None,
        None,
        None,
        backend.manifest_sha256 if isinstance(backend, OwlProfileBackend) else "",
        None,
        (),
        Report("owl.backend", issues=tuple(issues), requested=1, complete=False),
    )
