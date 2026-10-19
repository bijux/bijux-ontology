"""Required explicit ShEx bindings with one owned native process and paid receipts."""

import json
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rdflib import Graph, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, BudgetKind, IssueCollector, Limits
from bijux_ontology.execution.processes.transport import run_worker
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource

from . import preparation
from .types import ShexBinding, ShexObligation, ShexResult, ShexSchema

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def _counts(raw: object, grant: dict[BudgetKind, int]) -> dict[BudgetKind, int]:
    """Validate exact nonnegative finite receipts before cumulative adoption."""
    if not isinstance(raw, dict) or set(raw) != set(grant):
        raise InputError("Malformed ShEx worker receipt")
    result: dict[BudgetKind, int] = {}
    for kind in _KINDS:
        value = raw[kind]
        if type(value) is not int or not 0 <= value <= grant[kind]:
            raise InputError("ShEx worker exceeded its supplied allowance")
        result[kind] = value
    return result


def _read(path: Path, cap: int, limits: Limits) -> Any:
    """Read only bounded regular owned protocol records after worker cleanup."""
    with path.open("rb") as handle:
        raw = handle.read(cap + 1)
    return decode_json(raw, max_bytes=cap, max_depth=limits.max_depth)


def validate_shex(
    data: Graph | PreparedSource,
    schema: ShexSchema,
    *,
    bindings: Sequence[ShexBinding] = (),
    imports: Mapping[str, ShexSchema] | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
    artifacts: Path | None = None,
) -> ShexResult:
    """Validate supplied native ShapeDecl ShExJ using optional pyrudof 0.3.20.

    Named absolute focus/shape IRIs and positive structural expressions are the
    qualified profile. Other RDF focus terms, dialects, extension actions and
    scalar domains remain unknown. Integer literals with XML whitespace or more
    than eighteen digits exceed the native profile. Imports resolve exact supplied
    documents. Raw native JSON, RDF lexical forms and declared selection survive;
    there is no SHACL translation, discovery, inference or semantic correction.

    Host transport capacity is reserved before native work. Normal receipts adopt
    actual controlled costs; interrupted/malformed workers forfeit their remaining
    grant. Opaque native CPU is governed by the process deadline, rather than an
    invented semantic-work counter. Completed paid binding observations survive
    cancellation and later exhaustion, while every unfinished entry stays unknown.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    requested = len(bindings) if isinstance(bindings, Sequence) else 1
    captured: tuple[ShexBinding, ...] = ()
    obligations: dict[int, ShexObligation] = {}
    closure: tuple[str, ...] = ()
    scope = ""
    backend = ""
    finished = False
    diagnostics_json: str | None = None
    protocol_reserved = False
    try:
        captured = preparation.bindings(bindings, operation)
        requested = len(captured)
        if not captured:
            raise InputError("Required ShEx validation needs an explicit nonempty map")
        if not isinstance(data, Graph | PreparedSource):
            raise InputError("ShEx data requires an RDF graph or prepared selection")
        native_schema, closure, ids = preparation.schemas(schema, imports, operation)
        rows, scope = preparation.data(data, graph_name, union, operation)
        pairs: list[object] = []
        for index, binding in enumerate(captured):
            operation.consume("work")
            supported = isinstance(binding.focus, URIRef) and isinstance(
                binding.shape, URIRef
            )
            if not supported:
                issues.append(
                    Issue(
                        "shex.unsupported",
                        "Named RDF focus and shape IRIs are required",
                        str(index),
                        kind="unverified",
                    )
                )
                pairs.append(None)
            elif str(binding.shape) not in ids:
                raise InputError("Requested ShEx shape is not an authored declaration")
            else:
                pairs.append([str(binding.focus), str(binding.shape)])
        count = len(captured)
        # Reserve bounded host decoding, result ownership and issue references
        # before granting native work. Receipt adoption remains valid after stop.
        cap = min(
            operation.limits.max_bytes,
            operation.remaining("work") // 16,
            operation.remaining("bytes") // 128,
            operation.remaining("states") // 2,
        )
        if cap < 1024:
            raise LimitExceeded("Insufficient ShEx protocol capacity")
        operation.consume("bytes", 64 * cap + 128 * count)
        operation.consume("work", 8 * cap + 16 * count)
        operation.consume("states", cap + 4 * count)
        operation.consume("issues", count + 1)
        protocol_reserved = True
        grant = {kind: operation.remaining(kind) for kind in _KINDS}
        grant["issues"] = 0
        request = {
            "schema": native_schema,
            "data": rows,
            "bindings": pairs,
            "limits": vars(operation.limits),
            "allowances": grant,
        }
        size = json_bound(request, operation)
        operation.consume("bytes", size)
        if size > cap:
            raise LimitExceeded("ShEx request exceeds the bounded protocol capacity")
        # Request framing is host work and must precede the final native grant.
        grant = {kind: operation.remaining(kind) for kind in _KINDS}
        grant["issues"] = 0
        request["allowances"] = grant
        payload = json.dumps(request, ensure_ascii=False).encode()
        root = (
            artifacts
            if artifacts is not None
            else Path.cwd() / "artifacts" / "ontology-shex"
        )
        if not isinstance(root, Path):
            raise InputError("ShEx artifacts require an owned Path")
        root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="shex-", dir=root) as private:
            directory = Path(private)
            (directory / "request.json").write_bytes(payload)
            code, stopped, _detail = run_worker(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "bijux_ontology.shex.worker",
                    str(directory.resolve()),
                    str(cap),
                ],
                timeout=max(0.001, operation.deadline - time.monotonic()),
                cap=cap,
                cancellation=cancel,
                deadline=lambda: operation.deadline,
            )
            paid = dict.fromkeys(_KINDS, 0)
            projected: list[Issue] = []
            problem = "shex.execution_incomplete" if stopped else "shex.engine_error"
            complete = False
            malformed = False
            try:
                path = directory / "completed.jsonl"
                if path.exists():
                    with path.open("rb") as handle:
                        raw = handle.read(cap + 1)
                    if len(raw) > cap:
                        raise InputError(
                            "ShEx completed results exceed their reserved cap"
                        )
                    expected = [
                        index for index, pair in enumerate(pairs) if pair is not None
                    ]
                    for line in raw.splitlines(keepends=True):
                        if not line.endswith(b"\n"):
                            break
                        record = object_value(
                            decode_json(
                                line,
                                max_bytes=cap,
                                max_depth=operation.limits.max_depth,
                            ),
                            location="ShEx receipt",
                        )
                        raw_index = record.get("index")
                        if (
                            type(raw_index) is not int
                            or not expected
                            or raw_index != expected.pop(0)
                        ):
                            raise InputError(
                                "ShEx receipts changed requested binding order"
                            )
                        index = raw_index
                        receipt = _counts(record.get("consumed"), grant)
                        if any(receipt[kind] < paid[kind] for kind in _KINDS):
                            raise InputError(
                                "ShEx receipts regressed cumulative accounting"
                            )
                        native = record.get("native")
                        binding = captured[index]
                        if (
                            record.get("backend") != "pyrudof 0.3.20"
                            or not isinstance(native, dict)
                            or native.get("node") != str(binding.focus)
                            or native.get("shape") != str(binding.shape)
                            or native.get("status")
                            not in ("conformant", "nonconformant")
                        ):
                            raise InputError(
                                "Native ShEx result lacks exact binding coverage"
                            )
                        matched = native["status"] == "conformant"
                        obligations[index] = ShexObligation(
                            binding,
                            "matched" if matched else "not_matched",
                            json.dumps(native, ensure_ascii=False),
                        )
                        if not matched:
                            projected.append(
                                Issue(
                                    "shex.not_matched",
                                    "The native ShEx obligation did not match",
                                    str(index),
                                )
                            )
                        paid = receipt
                        backend = "pyrudof 0.3.20"
                terminal = object_value(
                    _read(directory / "result.json", cap, operation.limits),
                    location="ShEx terminal",
                )
                receipt = _counts(terminal.get("consumed"), grant)
                if any(receipt[kind] < paid[kind] for kind in _KINDS):
                    raise InputError("ShEx terminal receipt regressed accounting")
                paid = receipt
                raw_backend = terminal.get("backend", "")
                if not isinstance(raw_backend, str) or raw_backend not in (
                    "",
                    "pyrudof 0.3.20",
                ):
                    raise InputError("Unexpected ShEx backend identity")
                backend = raw_backend
                diagnostic = terminal.get("error")
                if diagnostic is not None:
                    if (
                        not isinstance(diagnostic, dict)
                        or set(diagnostic) != {"type", "message"}
                        or any(
                            not isinstance(value, str) for value in diagnostic.values()
                        )
                    ):
                        raise InputError("Malformed native ShEx diagnostic")
                    diagnostics_json = json.dumps(diagnostic, ensure_ascii=False)
                raw_problem = terminal.get("status", "shex.engine_error")
                if not isinstance(raw_problem, str) or raw_problem not in (
                    "complete",
                    "shex.backend_unavailable",
                    "shex.input_invalid",
                    "shex.execution_incomplete",
                    "shex.engine_error",
                ):
                    raise InputError("Unexpected ShEx terminal status")
                problem = raw_problem
                complete = (
                    not stopped
                    and not code
                    and problem == "complete"
                    and len(obligations) == sum(pair is not None for pair in pairs)
                )
            except (InputError, OSError):
                malformed = True
            finished = complete
            adopted = grant if stopped or code or malformed else paid
            for kind, amount in adopted.items():
                operation.shared.consume(kind, amount)
            # These findings and their metadata fit the host reservation made
            # before dispatch. Retain them without re-running a stopped deadline.
            for issue in projected:
                list.append(issues, issue)
            if not complete:
                if stopped or problem == "shex.execution_incomplete":
                    issues.exhausted("Owned ShEx execution did not complete")
                else:
                    list.append(
                        issues,
                        Issue(
                            problem if problem != "complete" else "shex.engine_error",
                            "Install bijux-ontology[shex] for native validation"
                            if problem == "shex.backend_unavailable"
                            else "Required native ShEx execution is unavailable "
                            "or incomplete",
                            kind="input_error"
                            if problem == "shex.input_invalid"
                            else "unverified",
                        ),
                    )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except preparation.Unsupported as error:
        try:
            issues.append(Issue("shex.unsupported", str(error), kind="unverified"))
        except LimitExceeded:
            issues.exhausted("ShEx capability diagnostics exceeded the allowance")
    except (InputError, OSError) as error:
        try:
            issues.append(Issue("shex.input_invalid", str(error), kind="input_error"))
        except LimitExceeded:
            issues.exhausted("ShEx input diagnostic exceeded the shared allowance")
    result = tuple(
        obligations.get(index, ShexObligation(binding, "unknown"))
        for index, binding in enumerate(captured)
    )
    if finished and (
        time.monotonic() >= operation.deadline or cancel is not None and cancel.is_set()
    ):
        issues.exhausted("ShEx execution stopped during final result handoff")
        finished = False
    report = Report(
        "shex",
        len(obligations),
        tuple(issues),
        requested=requested,
        complete=finished and requested > 0 and len(obligations) == requested,
    )
    if protocol_reserved:
        operation.shared._register_transferred_report(
            report,
            paid_issues=operation.shared._counts.get("issues", 0),
            max_references=4 * len(captured) + 64,
        )
    return ShexResult(
        result,
        report,
        schema.document_iri if isinstance(schema, ShexSchema) else "",
        closure,
        scope,
        backend,
        diagnostics_json,
    )
