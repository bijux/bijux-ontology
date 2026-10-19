"""Explicit opt-in preparation of selected HTTP closures into ordinary bundles."""

import base64
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict
from importlib.resources.abc import Traversable
from itertools import chain
from pathlib import Path
from typing import Literal, cast

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import (
    InputError,
    InputFailure,
    LimitExceeded,
    SourceSpan,
)
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.documents.archive import bundle_reader, resource_path
from bijux_ontology.documents.writing import write_bytes
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.workflows.projects.configuration import ProjectConfig

from .types import (
    AcquiredResource,
    AcquisitionPolicy,
    AcquisitionRequest,
    BundleAcquisition,
)


def _failure(value: object) -> InputFailure | None:
    """Read actual typed source evidence from this operation's owned worker."""
    if value is None:
        return None
    data = object_value(value, location="acquisition")
    span = data.get("span")
    position = None
    if span is not None:
        part = object_value(span, location="acquisition span")
        position = SourceSpan(
            cast(int | None, part.get("start_line")),
            cast(int | None, part.get("start_column")),
            cast(int | None, part.get("end_line")),
            cast(int | None, part.get("end_column")),
            column_unit=cast(
                Literal["unicode_code_points", "utf16_code_units"] | None,
                part.get("column_unit"),
            ),
        )
    return InputFailure(
        cast(str | None, data.get("document_iri")),
        cast(str, data.get("location", "")),
        cast(str, data.get("role", "input")),
        tuple(cast(list[str], data.get("import_chain", []))),
        position,
    )


def acquire_bundle(
    source: Path | Traversable,
    *,
    requests: Sequence[AcquisitionRequest],
    policy: AcquisitionPolicy,
    destination: Path,
    limits: Limits | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> BundleAcquisition:
    """Acquire only an explicitly selected closure; ordinary checks stay offline.

    The source is an ordinary project bundle skeleton with caller-authored jobs
    and local inputs. Each request names one declared missing member. Resources
    and parsers in the owned worker share one cumulative budget and real deadline.
    Host capture and final ordinary load_bundle qualification have separate Limits
    boundaries; this operation does not claim a single cumulative parser budget
    across those independent normal loaders. Both parsers run in the worker; its
    actual deadline also covers ordinary final qualification. The deadline covers
    bounded host checkpoints. Filesystem publication uses ordinary OS I/O and is
    not hard-preempted. No credentials, cookies or environment proxies are
    discovered or forwarded. Successful preparation establishes availability for
    selected closures, not acceptance of every configured application check.
    """
    if not isinstance(policy, AcquisitionPolicy) or not isinstance(destination, Path):
        raise InputError(
            "Acquisition requires a typed policy and explicit destination Path"
        )
    if destination.exists():
        raise InputError("Acquisition destination must be a new file")
    operation = Operation(limits, None, timeout, cancel)
    issues = IssueCollector(operation)
    records: list[AcquiredResource] = []
    output = None
    requested = 1
    try:
        bound_text(str(destination), operation)
        selected = capture_items(requests, AcquisitionRequest, operation)
        if not selected:
            raise InputError("Acquisition requires explicit nonempty requests")
        requested = len(selected)
        lookup = {}
        for selection in selected:
            bound_text(selection.path, operation)
            bound_text(selection.url, operation)
            if selection.path == "project.json" or selection.path in lookup:
                raise InputError("Acquisition paths must be distinct resource members")
            lookup[selection.path] = selection.url
        for origin in policy.origins:
            bound_text(origin, operation)
        with bundle_reader(source, limits=operation.limits) as read:
            manifest = object_value(
                decode_json(
                    read("project.json"),
                    max_bytes=operation.limits.max_bytes,
                    max_depth=operation.limits.max_depth,
                ),
                location="acquisition",
            )
            config = ProjectConfig(manifest, directory=Path.cwd())
            paths = set()
            for input_path in config.input_paths():
                operation.consume("work")
                try:
                    relative = input_path.relative_to(config.directory).as_posix()
                except ValueError as error:
                    raise InputError(
                        "Acquisition input paths must remain within the bundle"
                    ) from error
                bound_text(relative, operation)
                paths.add(resource_path(relative))
            if isinstance(source, Path) and source.is_dir():
                bound_text(str(source), operation)
                root = source.resolve()
                chosen = destination.resolve()
                root_text = str(root)
                bound_text(root_text, operation)
                bound_text(str(chosen), operation)
                root_size = len(root_text.encode("utf-8"))
                for member in chain(("project.json",), paths):
                    bound_text(member, operation)
                    size = root_size + 1 + len(member.encode("utf-8"))
                    if size > operation.limits.max_bytes:
                        raise LimitExceeded("Acquisition source path exceeds max_bytes")
                    operation.consume("bytes", size)
                    operation.consume("states")
                    original = (root / member).resolve()
                    bound_text(str(original), operation)
                    if chosen == original:
                        raise InputError(
                            "Acquisition output cannot replace a declared source member"
                        )
            declared_resources = set()
            for raw_entry in object_value(
                manifest.get("rdf", {}), location="rdf"
            ).values():
                entry = object_value(raw_entry, location="rdf resource")
                declared_resources.add(resource_path(entry.get("path")))
            for raw_entry in object_value(
                manifest.get("contexts", {}), location="contexts"
            ).values():
                value = (
                    raw_entry
                    if isinstance(raw_entry, str)
                    else object_value(raw_entry, location="context resource").get(
                        "path"
                    )
                )
                declared_resources.add(resource_path(value))
            if set(lookup) - declared_resources:
                raise InputError(
                    "Acquisition requests must name declared RDF/context resources"
                )
            if set(lookup) - paths:
                raise InputError(
                    "Acquisition requests must name declared input members"
                )
            files = {}
            for path in paths:
                if path in lookup:
                    continue
                payload = read(path)
                operation.consume("resources")
                operation.consume("bytes", len(payload))
                operation.consume("bytes", 4 * ((len(payload) + 2) // 3))
                files[path] = base64.b64encode(payload).decode("ascii")
        cap = 4 * operation.limits.max_total_bytes + 4096
        operation.consume("work", sum(len(value) for value in files.values()))
        raw = {
            "manifest": manifest,
            "files": files,
            "requests": lookup,
            "policy": asdict(policy),
            "limits": asdict(operation.limits),
            "timeout": max(0.001, operation.deadline - time.monotonic()),
        }
        for amount in asdict(operation.limits).values():
            if amount.bit_length() > 63:
                raise InputError("Acquisition limits exceed machine-sized capacities")
        reserved = json_bound(manifest, operation) + 8192
        reserved += sum(
            12 * len(name) + len(value) + 16 for name, value in files.items()
        )
        reserved += sum(
            12 * (len(name) + len(value)) + 16 for name, value in lookup.items()
        )
        reserved += sum(12 * len(origin) + 8 for origin in policy.origins)
        operation.consume("bytes", reserved)
        payload = json.dumps(raw, ensure_ascii=True, separators=(",", ":")).encode()
        if len(payload) > cap:
            raise LimitExceeded("Acquisition input exceeds protocol cap")
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("Acquisition exceeded its deadline")
        response = execute_worker(
            "bijux_ontology.workflows.projects.acquisition.worker",
            payload,
            cap=cap,
            timeout=remaining,
            cancel=cancel,
        )
        operation.consume("bytes", len(response))
        result = object_value(
            decode_json(response, max_bytes=cap, max_depth=operation.limits.max_depth),
            location="acquisition",
        )
        raw_records = result.get("resources")
        if not isinstance(raw_records, list):
            raise InputError("Acquisition worker requires resource evidence")
        for raw_record in raw_records:
            operation.consume("states")
            data = object_value(raw_record, location="acquisition")
            for name in (
                "requested_url",
                "effective_url",
                "media_type",
                "encoding",
                "sha256",
            ):
                if not isinstance(data.get(name), str):
                    raise InputError("Malformed acquisition resource metadata")
            for name in ("compressed_bytes", "expanded_bytes"):
                amount = data.get(name)
                if type(amount) is not int or amount < 0:
                    raise InputError("Malformed acquisition byte counts")
            if data.get("context_url") is not None and not isinstance(
                data["context_url"], str
            ):
                raise InputError("Malformed acquisition context link")
            record = AcquiredResource(
                cast(str, data["requested_url"]),
                cast(str, data["effective_url"]),
                cast(str, data["media_type"]),
                cast(str | None, data.get("context_url")),
                cast(str, data["encoding"]),
                cast(int, data["compressed_bytes"]),
                cast(int, data["expanded_bytes"]),
                cast(str, data["sha256"]),
            )
            for value in asdict(record).values():
                if isinstance(value, str):
                    bound_text(value, operation)
            records.append(record)
        raw_error = result.get("error")
        if raw_error is not None:
            stopped = object_value(raw_error, location="acquisition")
            kind = stopped.get("kind")
            if kind not in ("input_error", "exhausted", "unverified"):
                raise InputError("Invalid acquisition worker error kind")
            item = Issue(
                "execution.limit_exceeded"
                if kind == "exhausted"
                else "acquisition.resource_failed"
                if kind == "input_error"
                else "acquisition.unavailable",
                cast(str, stopped.get("message")),
                cast(str, stopped.get("location", "")),
                kind=kind,
                details=Location(failure=_failure(stopped.get("failure"))),
            )
            retain_report(
                Report("acquisition.resource", 0, (item,), requested=1),
                operation,
                issues,
            )
        else:
            encoded = result.get("archive")
            if not isinstance(encoded, str):
                raise InputError("Successful acquisition requires an ordinary archive")
            operation.consume("work", len(encoded))
            archive = base64.b64decode(encoded, validate=True)
            if len(archive) > operation.limits.max_total_bytes:
                raise LimitExceeded("Acquired archive exceeds max_total_bytes")
            operation.consume("work")
            write_bytes(archive, destination)
            output = destination
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            item = Issue(
                "acquisition.resource_failed",
                str(error),
                details=Location.from_error(error),
                kind="input_error",
            )
            retain_report(
                Report("acquisition.input", 0, (item,), requested=1), operation, issues
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return BundleAcquisition(
        output,
        tuple(records),
        Report(
            "acquisition.bundle",
            requested if output is not None else 0,
            tuple(issues),
            requested=requested,
            regime="selected-offline-resource-availability",
        ),
    )
