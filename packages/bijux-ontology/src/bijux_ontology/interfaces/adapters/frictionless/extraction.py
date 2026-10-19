"""Checked RDF from actual optional package rows and explicit semantic bindings."""

import json
import math
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from rdflib import Graph

from bijux_ontology._json import _measure, decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.processes.transport import checkpoint, exchange_worker
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource

from .profile import UnsupportedPackage, array, capture, object_value, text
from .projection import bindings as capture_bindings
from .projection import rows as project_rows
from .types import PackageCell, PackageExtraction, PackageProducer, ResourceBinding

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def _run(
    descriptor: dict[str, object],
    directory: str,
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> dict[str, object]:
    """Delegate native allowances once, retaining no credit after a missing receipt."""
    request: dict[str, object] = {
        "descriptor": descriptor,
        "directory": directory,
        "limits": {
            field.name: getattr(budget.limits, field.name) for field in fields(Limits)
        },
        "allowances": dict.fromkeys(_KINDS, 0),
        "max_errors": max(1, budget.remaining("issues")),
        "output_cap": 1,
    }
    size = _measure(request, budget)
    budget.consume("bytes", 32 * size + 8192)
    cap = min(
        budget.limits.max_bytes,
        budget.remaining("bytes") // 128,
        budget.remaining("work") // 8,
        budget.remaining("states") // 2,
    )
    if cap < 1024:
        raise LimitExceeded(
            "Package native response has no reserved adoption allowance"
        )
    allowances = {key: budget.remaining(key) // 4 for key in _KINDS}
    request["allowances"] = allowances
    request["output_cap"] = cap
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    checkpoint(deadline, cancel)
    dispatched = received = False
    try:
        dispatched = True
        raw = exchange_worker(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology.interfaces.adapters.frictionless.worker",
                str(max(cap, len(payload))),
            ],
            payload,
            cap=max(cap, len(payload)),
            timeout=max(0.000001, deadline - time.monotonic()),
            cancel=cancel,
        )
        checkpoint(deadline, cancel)
        if len(raw) > cap:
            raise ResourceError("Native package response exceeded its reserved cap")
        budget.consume("bytes", 32 * len(raw) + 1024)
        budget.consume("work", len(raw) + 1)
        reply = object_value(
            decode_json(raw, max_bytes=cap, max_depth=budget.limits.max_depth)
        )
        if (
            set(reply)
            != {
                "protocol",
                "status",
                "producer",
                "valid",
                "complete",
                "warnings",
                "rows",
                "errors",
                "message",
                "controls",
            }
            or type(reply["protocol"]) is not int
            or reply["protocol"] != 1
            or reply["status"]
            not in ("completed", "exhausted", "unavailable", "failed")
            or type(reply["complete"]) is not bool
            or type(reply["warnings"]) is not list
            or type(reply["valid"]) is not bool
            or type(reply["rows"]) is not list
            or type(reply["errors"]) is not list
            or type(reply["message"]) is not str
        ):
            raise ResourceError(
                "Native package returned an invalid observation receipt"
            )
        consumed = object_value(reply["controls"])
        if set(consumed) != set(_KINDS):
            raise ResourceError("Native package omitted cumulative counters")
        for key in _KINDS:
            if (
                type(consumed[key]) is not int
                or not 0 <= cast(int, consumed[key]) <= allowances[key]
            ):
                raise ResourceError(
                    "Native package returned an invalid cumulative counter"
                )
        for key in _KINDS:
            budget.consume(key, cast(int, consumed[key]))
        received = True
        return reply
    finally:
        if dispatched and not received:
            for key in _KINDS:
                budget.consume(key, allowances[key])


def _native_errors(values: object, budget: Budget, issues: IssueCollector) -> None:
    """Retain native codes and actual resource/record/field locations."""
    for raw in array(values):
        envelope = object_value(raw)
        path = envelope["path"]
        if type(path) is not str:
            raise ResourceError(
                "Native error omitted its package-relative resource identity"
            )
        error = object_value(envelope["error"])
        code = text(error.get("type"))
        message = text(error.get("message"))
        bound_text(code, budget)
        bound_text(message, budget)
        row, number, name = (
            error.get("rowNumber"),
            error.get("fieldNumber"),
            error.get("fieldName"),
        )
        selected = error.get("fieldNames")
        if name is None and isinstance(selected, list) and len(selected) == 1:
            name = selected[0]
        location: tuple[str | int, ...] = ()
        if type(row) is int and row >= 0:
            location = (row,)
        if type(name) is str:
            location += (name,)
        elif type(number) is int and number >= 0:
            location += (number,)
        details = Location(source=path, path=location)
        issues.append(
            Issue(
                "frictionless.native_" + code.replace("-", "_"),
                message,
                str(details),
                details=details,
            )
        )


def extract_package(
    description: object,
    resources: Mapping[str, bytes],
    bindings: tuple[ResourceBinding, ...],
    *,
    work_directory: Path,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PackageExtraction:
    """Qualify a supplied v1 CSV package through Frictionless 5.21.0.

    Only explicit inline schemas, scalar string/integer keys, default scalar
    types and the documented finite constraints/dialect are supported. Unknown
    descriptor semantics remain required unknown. Original resource paths name
    locations; native record ordinals are not physical lines. Missing native
    values emit no RDF. Selected rows are checked separately before union.

    Native parsing and key lookups run in a reaped process with a hard deadline.
    Explicit control counters do not claim to meter opaque framework internals.
    The caller supplies an existing work directory; an exclusive owned child is
    removed after reaping, while existing consumer files remain untouched.
    No schema, descriptor or table is implicitly retrieved.
    """
    if (
        not isinstance(work_directory, Path)
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise InputError(
            "Use an existing explicit work_directory and positive finite timeout"
        )
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Cancellation requires threading.Event")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Package limits require Limits")
    current = operation(limits, budget)
    issues = IssueCollector(current)
    native_issues = IssueCollector(current)
    cells: list[PackageCell] = []
    checks: list[Report] = []
    producer = None
    source: PreparedSource | None = None
    native_complete = finished = False
    checked = 0
    deadline = time.monotonic() + timeout
    graph = Graph(bind_namespaces="none")
    try:
        checkpoint(deadline, cancel)
        descriptor, supplied, entries = capture(description, resources, current)
        selected = capture_bindings(bindings, entries, current)
        bound_text(str(work_directory), current)
        if not work_directory.is_dir():
            raise InputError("Package work_directory must already exist")
        with TemporaryDirectory(
            dir=work_directory, prefix="frictionless-"
        ) as directory:
            for relative, content in supplied.items():
                checkpoint(deadline, cancel)
                destination = Path(directory) / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    output.write(content)
            response = _run(descriptor, directory, current, deadline, cancel)
        raw_producer = response["producer"]
        if raw_producer is not None:
            identity = object_value(raw_producer)
            if (
                set(identity) != {"framework", "python", "standard"}
                or identity["framework"] != "5.21.0"
                or identity["standard"] != "v1"
            ):
                raise ResourceError("Native package producer identity is not qualified")
            python = text(identity["python"])
            bound_text(python, current)
            producer = PackageProducer("5.21.0", python)
        _native_errors(response["errors"], current, native_issues)
        for raw_warning in array(response["warnings"]):
            warning = object_value(raw_warning)
            message = text(warning["message"])
            path = warning["path"]
            if type(path) is not str:
                raise ResourceError("Native warning omitted resource scope")
            native_issues.append(
                Issue(
                    "frictionless.native_coverage_unverified",
                    message,
                    path,
                    kind="unverified",
                )
            )
        issues.extend(native_issues)
        native_complete = (
            response["status"] == "completed"
            and producer is not None
            and response["complete"] is True
        )
        valid = native_complete and response["valid"] is True and not native_issues
        checked = project_rows(
            response["rows"],
            entries,
            selected,
            current,
            cells,
            checks,
            issues,
            graph,
            deadline,
            cancel,
            check=valid,
        )
        if response["status"] == "exhausted":
            raise LimitExceeded(
                "Native package controls exceeded the delegated allowance"
            )
        if response["status"] == "unavailable":
            raise ResourceError(text(response["message"]))
        if not native_complete:
            raise ResourceError(
                text(response["message"])
                if response["message"]
                else "Native package validation/reading did not complete"
            )
        if valid and checked == 0:
            raise UnsupportedPackage(
                "No actual selected native rows establish semantic extraction"
            )
        if valid:
            candidate = prepare_rdf(graph, budget=current)
            checkpoint(deadline, cancel)
            source = candidate
        finished = True
    except (LimitExceeded, RecursionError) as error:
        issues.retain_completed(Report("native-prefix", 0, tuple(native_issues)))
        issues.exhausted(str(error))
    except (InputError, OSError, ValueError) as error:
        issues.retain_completed(Report("native-prefix", 0, tuple(native_issues)))
        try:
            issues.append(
                Issue(
                    "frictionless.extraction_unverified"
                    if isinstance(error, ResourceError)
                    else "frictionless.input_invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    native = Report(
        "frictionless-native-package",
        int(native_complete),
        tuple(native_issues if native_complete else issues),
        requested=1,
        complete=native_complete,
        regime="frictionless-5.21.0-v1",
    )
    report = Report(
        "frictionless-extraction",
        int(finished),
        tuple(issues),
        requested=1,
        complete=finished,
        regime="explicit-package-semantic-bindings",
        children=(native, *checks),
    )
    return PackageExtraction(
        source, tuple(cells), producer, native, tuple(checks), report
    )
