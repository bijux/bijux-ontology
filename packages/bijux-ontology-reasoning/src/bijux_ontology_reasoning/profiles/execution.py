"""Owned JVM execution and typed OWLAPI profile-result qualification."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any, cast

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.processes.transport import run_worker
from bijux_ontology.rdf.patches.correspondence import Operation, _json_bound
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.rdf.stores.seals import verify_files
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from .preparation import (
    LOADER_PROFILE,
    MAIN_CLASS,
    OWLAPI_VERSION,
    PROFILES,
    qualify_backend,
    stage_documents,
)
from .types import (
    BackendCapability,
    LoaderQualification,
    OwlProfile,
    OwlProfileBackend,
    ProfileCheck,
    ProfileDocument,
    ProfileViolation,
)


def _integer(value: object, budget: Budget, maximum: int) -> int:
    """Qualify native counters before formatting or exposing retained metadata."""
    if type(value) is not int or value < 0 or value > maximum:
        raise ResourceError("Invalid OWL profile result counter")
    budget.consume("work")
    return value


def _text(value: object, budget: Budget, *, optional: bool = False) -> str | None:
    """Bound typed worker text before diagnostic or result retention."""
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ResourceError("Invalid OWL profile result text")
    bound_text(value, budget)
    return value


def _capability(
    data: dict[str, Any],
    backend: OwlProfileBackend,
    operation: Budget,
) -> BackendCapability:
    """Accept only the pinned actual runtime and four maintained checker identities."""
    if type(data.get("protocol")) is not int or data["protocol"] != 1:
        raise ResourceError("Unsupported OWL worker protocol")
    version = _text(data.get("owlapi"), operation)
    java = _text(data.get("java"), operation)
    vm = _text(data.get("vm"), operation)
    pin = _text(data.get("manifest_sha256"), operation)
    configuration = _text(data.get("loader_profile"), operation)
    major = _integer(data.get("java_major"), operation, 1000)
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != len(PROFILES):
        raise ResourceError("OWL worker omitted its profile capabilities")
    operation.consume("states", len(profiles))
    for value in profiles:
        _text(value, operation)
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


def _invoke(
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
    operation.consume("bytes", _json_bound(request, operation))
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
    status = _text(data.get("status"), operation)
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
            data = _invoke(
                backend, {"operation": "capability"}, Path(job), operation, heap_mb
            )
            return _capability(data, backend, operation)
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


def check_profile(
    backend: OwlProfileBackend,
    source: PreparedSource,
    *,
    profile: OwlProfile,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ProfileCheck:
    """Check actual DL/EL/QL/RL membership through one pinned owned OWLAPI worker.

    Exact physical document scope and offline closure come from core preparation.
    Loader metadata gates input interpretation. No consistency, satisfiability,
    exact OWL/RDF roundtrip, hard RSS or native instruction-count claim is made.
    Heap and wall-clock limits apply to the disposable JVM; host work shares Budget.
    """
    if not isinstance(profile, str) or profile not in PROFILES:
        raise InputError("Choose one supported OWL2 profile")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    documents: tuple[ProfileDocument, ...] = ()
    loaders: list[LoaderQualification] = []
    violations: list[ProfileViolation] = []
    capability = None
    raw_answer = answer = None
    complete = False
    try:
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="profile-", dir=backend.artifacts) as job:
            documents, staged, _ = stage_documents(
                source, imports, Path(job), operation, issues, children
            )
            request = dict(
                staged,
                operation="profile",
                profile=profile,
                max_bytes=operation.limits.max_bytes,
                max_issues=min(
                    operation.remaining("issues"), operation.limits.max_states
                ),
                max_resources=operation.limits.max_resources,
            )
            if not request["max_issues"]:
                raise LimitExceeded("OWL profile has no remaining diagnostic allowance")
            data = _invoke(backend, request, Path(job), operation, heap_mb)
            operation.consume("states")
            capability = _capability(data, backend, operation)
            children.append(capability.report)
            if (
                data.get("profile") != profile
                or type(data.get("raw_in_profile")) is not bool
            ):
                raise ResourceError(
                    "OWL worker omitted its exact selected profile answer"
                )
            raw_answer = data["raw_in_profile"]
            actual_loaders = data.get("loaders")
            actual_violations = data.get("violations")
            if not isinstance(actual_loaders, list) or len(actual_loaders) != len(
                documents
            ):
                raise ResourceError("OWL worker omitted required loaded documents")
            if (
                not isinstance(actual_violations, list)
                or len(actual_violations) > operation.limits.max_issues
            ):
                raise ResourceError("OWL worker exceeded its violation inventory")
            operation.consume("states", len(actual_loaders) + len(actual_violations))
            seen = set()
            for record in actual_loaders:
                item = object_value(record, location="OWL worker record")
                index = _integer(item.get("document"), operation, len(documents) - 1)
                if index in seen or type(item.get("present")) is not bool:
                    raise ResourceError("OWL loader ownership is ambiguous")
                seen.add(index)
                present = cast(bool, item["present"])
                counts = tuple(
                    _integer(item.get(name), operation, operation.limits.max_triples)
                    if present
                    else None
                    for name in ("unparsed", "guessed", "triples")
                )
                header = _text(item.get("header"), operation, optional=not present)
                unparsed, guessed, triples = counts
                loader = LoaderQualification(
                    documents[index], present, unparsed, guessed, triples, header
                )
                loaders.append(loader)
                if not loader.qualified:
                    issues.append(
                        Issue(
                            "owl.loader_unverified",
                            "OWL parser metadata does not qualify the original RDF",
                            loader.document.document_iri or loader.document.identifier,
                            kind="unverified",
                        )
                    )
            qualified = all(loader.qualified for loader in loaders)
            if (
                raw_answer
                and actual_violations
                or not raw_answer
                and not actual_violations
                and data["status"] == "completed"
            ):
                raise ResourceError("OWL worker boolean contradicts its violations")
            if qualified and (data["status"] == "completed" or raw_answer is False):
                answer = raw_answer
            for record in actual_violations:
                item = object_value(record, location="OWL worker record")
                index = _integer(item.get("document"), operation, len(documents) - 1)
                kind = cast(str, _text(item.get("kind"), operation))
                axiom = _text(item.get("axiom"), operation, optional=True)
                expression = _text(item.get("expression"), operation, optional=True)
                violation = ProfileViolation(kind, documents[index], axiom, expression)
                violations.append(violation)
                if qualified:
                    issues.append(
                        Issue(
                            "owl.profile_violation",
                            "The ontology violates the selected OWL2 profile",
                            violation.document.document_iri
                            or violation.document.identifier,
                            expected=profile,
                            actual=kind,
                        )
                    )
            if data["status"] == "exhausted":
                raise LimitExceeded("OWL profile violation inventory is incomplete")
            complete = qualified
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.profile")
    except (InputError, OSError):
        answer = None
        try:
            issues.append(
                Issue(
                    "owl.profile_unverified",
                    "OWL profile execution or input interpretation is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.profile")
    report = Report(
        "owl.profile",
        int(complete),
        tuple(issues),
        requested=1,
        complete=complete,
        children=tuple(children),
        regime="owlapi-selected-profile",
        families=(profile,),
    )
    return ProfileCheck(
        profile,
        capability,
        answer,
        raw_answer,
        tuple(violations),
        documents,
        tuple(loaders),
        report,
    )
