"""Bounded owned SSSOM interchange with explicit caller metadata and no network."""

import base64
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker

from .types import MappingExport, MappingRead, MappingRow, MappingSet, _iri


def _text(value: object, operation: Operation) -> str:
    """Adopt a bounded scalar without accepting kind-erasing coercions."""
    if not isinstance(value, str):
        raise InputError("Mapping protocol requires textual values")
    bound_text(value, operation)
    return value


def _strings(value: object, operation: Operation) -> tuple[str, ...]:
    """Capture finite worker vectors before allocating owned tuples."""
    if not isinstance(value, list):
        raise InputError("Mapping protocol requires finite arrays")
    operation.consume("states", len(value))
    return tuple(_text(item, operation) for item in value)


def _adopt(
    data: dict[str, object],
    source: bytes,
    supplied_id: str,
    supplied_license: str,
    operation: Operation,
) -> MappingSet:
    """Qualify each exact row/cell before publishing the immutable domain record."""
    columns = _strings(data.get("columns"), operation)
    if len(set(columns)) != len(columns):
        raise InputError("Mapping columns must be unique")
    raw_rows, raw_expanded = data.get("rows"), data.get("expanded")
    if (
        not isinstance(raw_rows, list)
        or not isinstance(raw_expanded, list)
        or len(raw_rows) != len(raw_expanded)
    ):
        raise InputError("Mapping row witnesses require exact paired coverage")
    operation.consume("states", len(raw_rows))
    raw_types = data.get("row_types")
    if not isinstance(raw_types, list) or len(raw_types) != len(raw_rows):
        raise InputError("Mapping rows require exact declared-type coverage")
    rows = []
    for raw, expanded, raw_kind in zip(raw_rows, raw_expanded, raw_types, strict=True):
        cells, endpoints = _strings(raw, operation), _strings(expanded, operation)
        if len(cells) != len(columns) or len(endpoints) != 4:
            raise InputError("Mapping occurrence has inconsistent dimensions")
        for iri in endpoints:
            _iri(iri, operation)
        operation.consume("states", len(cells))
        kinds = _strings(raw_kind, operation)
        if len(kinds) != 3:
            raise InputError("Invalid mapping endpoint-type vector")
        for kind in kinds:
            if kind:
                _iri(kind, operation)
        rows.append(
            MappingRow(*endpoints, tuple(zip(columns, cells, strict=True)), *kinds)
        )
    raw_prefixes = data.get("prefixes")
    if not isinstance(raw_prefixes, list):
        raise InputError("Mapping prefixes require exact supplied bindings")
    operation.consume("states", len(raw_prefixes))
    prefixes = []
    for raw in raw_prefixes:
        pair = _strings(raw, operation)
        if len(pair) != 2:
            raise InputError("Invalid mapping prefix binding")
        _iri(pair[1], operation)
        prefixes.append((pair[0], pair[1]))
    metadata = _text(data.get("metadata"), operation)
    object_value(
        decode_json(
            metadata,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        ),
        location="mapping metadata",
    )
    encoded = _text(data.get("content"), operation)
    operation.consume("bytes", len(encoded))
    content = base64.b64decode(encoded, validate=True)
    if len(content) > operation.limits.max_bytes:
        raise LimitExceeded("Mapping TSV output exceeds max_bytes")
    return MappingSet._owned(
        rows=tuple(rows),
        columns=columns,
        prefixes=tuple(prefixes),
        metadata_json=metadata,
        source=source,
        _content=content,
        backend_version=_text(data.get("backend"), operation),
        schema_version=_text(data.get("schema"), operation),
        supplied_id=supplied_id,
        supplied_license=supplied_license,
        uninterpreted=_strings(data.get("uninterpreted"), operation),
    )


def read_sssom(
    source: bytes | Path,
    *,
    mapping_set_id: str,
    license: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> MappingRead:
    """Qualify an owned TSV subset using actual maintained schema and tooling.

    Explicit nonempty set identity/license are part of the supplied data and
    must agree with embedded metadata; generated backend defaults are rejected.
    All original lexical row occurrences are retained, including duplicates.
    '#' in the table body is unsupported; leading comment framing is SSSOM's.
    Parser/validator internals have owned deadline isolation, not invented work
    counters. Host preparation/adoption can share a caller Budget; worker
    preparation retains its independently bounded owned-process limits.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    result = None
    try:
        supplied_id, supplied_license = (
            _iri(mapping_set_id, operation),
            _iri(license, operation),
        )
        if isinstance(source, Path):
            bound_text(str(source), operation)
            with source.open("rb") as stream:
                content = stream.read(operation.limits.max_bytes + 1)
        elif isinstance(source, bytes):
            content = source
        else:
            raise InputError("SSSOM input requires owned bytes or a local Path")
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("SSSOM source exceeds max_bytes")
        operation.consume("bytes", len(content))
        operation.consume("work", len(content))
        operation.consume("bytes", 4 * ((len(content) + 2) // 3))
        request: dict[str, object] = {
            "source": base64.b64encode(content).decode("ascii"),
            "mapping_set_id": supplied_id,
            "license": supplied_license,
            "limits": asdict(operation.limits),
        }
        operation.consume("bytes", json_bound(request, operation))
        request["timeout"] = max(0.001, operation.deadline - time.monotonic())
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
        cap = 4 * operation.limits.max_total_bytes + 8192
        raw = execute_worker(
            "bijux_ontology.vocabulary.mappings.worker",
            payload,
            cap=cap,
            timeout=max(0.001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        operation.consume("bytes", len(raw))
        data = object_value(
            decode_json(raw, max_bytes=cap, max_depth=operation.limits.max_depth),
            location="mapping",
        )
        if "error" in data:
            kind = data["error"]
            if kind == "exhausted":
                raise LimitExceeded("SSSOM processing exhausted its configured bounds")
            if kind not in ("unverified", "input_error"):
                raise InputError("Unexpected SSSOM worker status")
            message = _text(data.get("message"), operation)
            issues.append(
                Issue(
                    "mapping.interchange_unverified"
                    if kind == "unverified"
                    else "mapping.invalid_input",
                    message,
                    kind=kind,
                )
            )
        else:
            result = _adopt(data, content, supplied_id, supplied_license, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, OSError, ValueError, ResourceError):
        try:
            issues.append(
                Issue(
                    "mapping.invalid_input",
                    "Owned SSSOM interchange could not complete",
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    return MappingRead(
        result,
        Report(
            "mapping.interchange",
            int(result is not None),
            tuple(issues),
            requested=1,
            regime="sssom-supported-tsv-interchange",
        ),
    )


def export_sssom(
    mapping_set: MappingSet,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    cancel: threading.Event | None = None,
) -> MappingExport:
    """Publish immutable bytes already qualified by maintained write and reparse.

    This is faithful table interchange, separately from check_mappings consumer
    policy truth. No mapping predicate is rewritten, inverted or interpreted as
    equivalence; export does not execute any semantic application policy.
    """
    operation = Operation(limits, budget, 30, cancel)
    issues = IssueCollector(operation)
    content = None
    try:
        if not isinstance(mapping_set, MappingSet):
            raise InputError("Export requires an owned read_sssom result")
        bound_text(mapping_set.metadata_json, operation)
        for row in mapping_set.rows:
            operation.consume("states")
            for name, value in row.cells:
                bound_text(name, operation)
                bound_text(value, operation)
        size = len(mapping_set._content)
        if size > operation.limits.max_bytes:
            raise LimitExceeded("Mapping TSV output exceeds max_bytes")
        operation.consume("bytes", size)
        operation.consume("work", size)
        content = mapping_set._content
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError:
        try:
            issues.append(
                Issue(
                    "mapping.invalid_input",
                    "Export requires a qualified immutable mapping set",
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    return MappingExport(
        content,
        Report(
            "mapping.export",
            int(content is not None),
            tuple(issues),
            requested=1,
            regime="sssom-supported-tsv-interchange",
        ),
    )
