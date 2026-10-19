"""Owned Morph-KGC capability, exact mapping staging and bounded materialization."""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.documents.export import export_rdf
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.processes.transport import run_worker
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json, object_value

from .plan import _MappingPlan
from .worker import REQUIRED


def _call(
    python: Path,
    request: dict[str, object],
    directory: Path,
    operation: Operation,
    *,
    file_cap: int,
) -> dict[str, object]:
    """Invoke the concrete stdlib worker in the explicitly supplied environment."""
    bound_text(str(python), operation)
    if not python.is_absolute() or not python.is_file():
        raise ResourceError("Supply an absolute installed Morph-KGC Python interpreter")
    cap = min(16384, operation.limits.max_bytes)
    if cap < 512 or file_cap < 512:
        raise LimitExceeded(
            "Native materialization requires a bounded control allowance"
        )
    operation.consume("states", 4)
    operation.consume("resources")
    operation.consume("bytes", 6 * cap + json_bound(request, operation))
    payload = json.dumps(request, separators=(",", ":")).encode()
    if len(payload) > cap:
        raise LimitExceeded("Native control request exceeds its byte allowance")
    source, receipt = directory / "request.json", directory / "receipt.json"
    source.write_bytes(payload)
    command = [
        str(python),
        "-I",
        str(Path(__file__).with_name("worker.py")),
        str(source),
        str(receipt),
        str(file_cap),
    ]
    for value in command:
        bound_text(value, operation)
    code, stopped, _diagnostics = run_worker(
        command,
        timeout=max(0.000001, operation.deadline - time.monotonic()),
        cap=cap,
        cancellation=operation.cancel,
    )
    if stopped or code == -signal.SIGXFSZ:
        raise LimitExceeded("Native materialization stopped within its owned limits")
    if code != 0 or receipt.is_symlink() or not receipt.is_file():
        raise ResourceError("Native materializer did not return a complete receipt")
    with receipt.open("rb") as reader:
        raw = reader.read(cap + 1)
    if len(raw) > cap:
        raise LimitExceeded("Native completion receipt exceeds its byte allowance")
    data = object_value(
        decode_json(raw, max_bytes=cap, max_depth=16), location="Morph-KGC receipt"
    )
    operation.consume("bytes", json_bound(data, operation))
    if data.get("versions") != REQUIRED:
        raise ResourceError(
            "Native interpreter differs from the qualified pinned profile"
        )
    if data.get("status") == "exhausted":
        raise LimitExceeded(
            "Native materializer exhausted its actual file/memory bound"
        )
    if data.get("status") != "completed":
        raise ResourceError(
            "The actual materializer did not complete: "
            + str(data.get("error", "unavailable"))
        )
    return data


def materialize(
    plan: _MappingPlan,
    directory: Path,
    operation: Operation,
    issues: IssueCollector,
    children: list[Report],
) -> int:
    """Return the admitted artifact cap only after actual native completion."""
    database = directory / "snapshot.sqlite"
    exported = export_rdf(
        plan.mapping, format="turtle", graph_name=plan.mapping_graph, budget=operation
    )
    children.append(exported.report)
    retain_report(exported.report, operation, issues)
    if not exported.report.verified or exported.content is None:
        raise ResourceError("Original mapping could not be staged exactly")
    (directory / "mapping.ttl").write_bytes(exported.content)
    config = directory / "config.ini"
    content = "\n".join(
        (
            "[CONFIGURATION]",
            "output_format=N-QUADS",
            "number_of_processes=1",
            "logging_level=ERROR",
            "na_values=",
            "infer_sql_datatypes=no",
            "output_file=" + str(directory / "output"),
            "[DataSource1]",
            "mappings=" + str(directory / "mapping.ttl"),
            "db_url=sqlite:///" + database.as_uri() + "?mode=ro&immutable=1&uri=true",
            "",
        )
    )
    bound_text(content, operation)
    config.write_text(content)
    cap = min(operation.limits.max_bytes, 2**31 - 1, operation.remaining("bytes") // 8)
    if cap < 512:
        raise LimitExceeded("Materialization has no remaining native output allowance")
    operation.consume("bytes", cap + 1)
    _call(
        plan.backend.python,
        {"action": "materialize", "config": str(config)},
        directory,
        operation,
        file_cap=cap,
    )
    return cap


def read_output(directory: Path, cap: int) -> bytes:
    """Read only the native output storage already admitted before dispatch."""
    path = directory / "output.nq"
    if path.is_symlink() or not path.is_file():
        raise ResourceError(
            "Actual materializer did not create its owned N-Quads artifact"
        )
    with path.open("rb") as reader:
        nquads = reader.read(cap + 1)
    if len(nquads) > cap:
        raise LimitExceeded("Actual native output exceeds its byte allowance")
    return nquads
