"""Explicit local resources and one bounded maintained-toolkit batch exchange."""

import json
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, replace
from hashlib import sha256
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report, report_from_data
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    BUDGET_KINDS,
    Budget,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.execution.processes.transport import exchange_worker
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.versions import exact_requirement
from bijux_ontology.json.values import decode_json, object_value

from .types import BiolinkIdentity, BiolinkModel, BiolinkResource


def reserve(budget: Budget, count: int = 1) -> None:
    """Reserve retained records and references before allocation or publication."""
    budget.consume("work", count + 1)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count)


def resource(
    value: BiolinkResource, budget: Budget
) -> tuple[BiolinkResource, dict[str, object]]:
    """Bind captured bytes once to both actual toolkit input and its identity."""
    if type(value) is not BiolinkResource:
        raise InputError("Supply an ordinary immutable BiolinkResource")
    name, version, content, digest = (
        value.name,
        value.version,
        value.content,
        value.sha256,
    )
    for text in (name, version, digest):
        bound_text(text, budget)
    if len(content) > budget.limits.max_bytes:
        raise LimitExceeded("Biolink resource exceeds max_bytes")
    budget.consume("resources")
    budget.consume("work", len(content) + 1)
    budget.consume("bytes", 4 * len(content) + 256)
    if len(digest) != 64 or sha256(content).hexdigest() != digest:
        raise InputError("Biolink resource bytes do not match their declared SHA256")
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise InputError("Biolink resources require UTF-8 YAML") from error
    bound_text(text, budget)
    reserve(budget)
    return replace(value), {
        "name": name,
        "version": version,
        "content": text,
        "sha256": digest,
    }


def identity(value: object) -> BiolinkIdentity | None:
    """Read only the owned worker's exact native identity envelope."""
    if value is None:
        return None
    data = object_value(value, location="Biolink identity")
    fields = {
        "model_version",
        "model_sha256",
        "predicate_sha256",
        "imports",
        "toolkit_version",
        "linkml_version",
        "python_version",
    }
    if set(data) != fields or any(
        type(data[k]) is not str for k in fields - {"imports"}
    ):
        raise ResourceError("Malformed Biolink runtime identity")
    imports = data["imports"]
    if type(imports) is not list or any(
        type(p) is not list or len(p) != 2 or any(type(x) is not str for x in p)
        for p in imports
    ):
        raise ResourceError("Malformed Biolink import identity")
    return BiolinkIdentity(
        cast(str, data["model_version"]),
        cast(str, data["model_sha256"]),
        cast(str, data["predicate_sha256"]),
        tuple((p[0], p[1]) for p in imports),
        cast(str, data["toolkit_version"]),
        cast(str, data["linkml_version"]),
        cast(str, data["python_version"]),
    )


def dispatch(
    request: dict[str, object],
    operation: Operation,
    *,
    expected: int,
) -> tuple[dict[str, object], Report]:
    """Reuse the owned process and cumulative receipt protocol without retries.

    Reply bytes, decoder workspace, reports and result references are prepaid
    before delegation. A missing/invalid receipt forfeits all delegated balances;
    native computation can never be retried with a fresh undepleted allowance.
    """
    reply_cap = min(operation.limits.max_bytes, max(8192, expected))
    request.update(
        limits=asdict(operation.limits),
        allowances=dict.fromkeys(BUDGET_KINDS, 0),
        output_cap=reply_cap,
    )
    size = json_bound(request, operation)
    operation.consume("bytes", 2 * size + 64 * reply_cap)
    operation.consume("states", 2 * reply_cap + 32)
    operation.consume("work", 4 * reply_cap + 1)
    delegated = DelegatedAllowance(
        operation, {key: operation.remaining(key) for key in BUDGET_KINDS}
    )
    request["allowances"] = delegated.to_data()
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    adopted = False
    try:
        raw = exchange_worker(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology_adapters.biolink.worker",
                str(max(len(payload), reply_cap)),
            ],
            payload,
            cap=max(len(payload), reply_cap),
            timeout=operation.deadline - time.monotonic(),
            cancel=operation.cancel,
        )
        if len(raw) > reply_cap:
            raise ResourceError(
                "Biolink worker exceeded its reserved response capacity"
            )
        data = object_value(
            decode_json(raw, max_bytes=reply_cap, max_depth=operation.limits.max_depth),
            location="Biolink worker",
        )
        if set(data) != {"identity", "interpretations", "report", "consumed"}:
            raise ResourceError("Malformed Biolink worker response")
        report = report_from_data(data["report"])
        # This is adoption of completed prepaid work, not a new native operation.
        # Preserve its negative findings even if the caller's deadline just passed.
        counts = delegated.settle(data["consumed"])
        operation._register_transferred_report(
            report,
            paid_issues=counts["issues"],
            max_references=2 * reply_cap,
        )
        adopted = True
        return data, report
    except InputError as error:
        raise LimitExceeded(
            "Biolink worker stopped without a qualified control receipt: " + str(error)
        ) from error
    finally:
        if not adopted:
            delegated.forfeit()


def failure(error: InputError, issues: IssueCollector) -> None:
    """Preserve paid findings when a later diagnostic cannot be retained."""
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error))
        return
    try:
        issues.append(
            Issue(
                "biolink.unverified"
                if isinstance(error, ResourceError)
                else "biolink.input_error",
                str(error),
                kind="unverified"
                if isinstance(error, ResourceError)
                else "input_error",
            )
        )
    except LimitExceeded as limit:
        issues.exhausted(str(limit))


def prepare_biolink(
    model: BiolinkResource,
    predicate_map: BiolinkResource,
    *,
    imports: Sequence[BiolinkResource] = (),
    require_bmt: str | None = None,
    require_linkml: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> BiolinkModel:
    """Qualify explicitly supplied local resources with maintained BMT/LinkML APIs.

    Every import, including linkml:types, must be supplied and byte-pinned. Native
    schema loading has an owned hard deadline; explicit counters meter adapter
    controls and allocation reservations, not opaque framework instructions/RSS.
    No model, predicate map or schema import falls back to a network default.
    """
    exact_requirement(require_bmt)
    exact_requirement(require_linkml)
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    first = second = None
    selected: tuple[BiolinkResource, ...] = ()
    native_identity = None
    native_report = None
    try:
        first, model_data = resource(model, operation)
        second, predicate_data = resource(predicate_map, operation)
        chosen = capture_items(imports, BiolinkResource, operation)
        values = []
        owned = []
        names = set()
        for item in chosen:
            current, data = resource(item, operation)
            if current.name in names:
                raise InputError("Biolink import bindings must be unique")
            reserve(operation)
            names.add(current.name)
            owned.append(current)
            values.append(data)
        selected = tuple(owned)
        request: dict[str, object] = {
            "mode": "prepare",
            "model": model_data,
            "predicate_map": predicate_data,
            "imports": values,
            "associations": [],
            "policy": {},
        }
        if require_bmt is not None:
            request["require_bmt"] = require_bmt
        if require_linkml is not None:
            request["require_linkml"] = require_linkml
        data, native_report = dispatch(request, operation, expected=8192)
        issues.retain_completed(native_report)
        native_identity = identity(data["identity"])
        operation.consume("work", 0)
    except InputError as error:
        failure(error, issues)
    if native_report is None:
        report = Report("biolink.preparation", 0, tuple(issues), requested=1)
    else:
        report = Report(
            "biolink.preparation",
            native_report.checked,
            tuple(issues),
            requested=native_report.requested,
            children=(native_report,),
            regime="biolink-selected-association",
        )
    return BiolinkModel._owned(first, second, selected, native_identity, report)
