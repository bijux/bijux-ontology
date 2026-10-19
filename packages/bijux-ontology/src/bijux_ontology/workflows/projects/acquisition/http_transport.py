"""One funded HTTP hop under the enclosing owned-process lifetime."""

import json
import os
import struct
import time
from dataclasses import asdict, dataclass
from typing import cast

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json

_REQUEST = struct.Struct("!IQQ")
_FRAME = struct.Struct("!4sBIIQ")
_U32 = struct.Struct("!I")
_MAGIC = b"BXH1"


@dataclass(frozen=True, slots=True)
class HopResponse:
    """Actual completed native headers/body, before host semantic admission."""

    kind: int
    effective: str
    target: str
    message: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _receipt(
    raw: bytes, header_cap: int, body_cap: int, budget: Budget
) -> tuple[HopResponse, object]:
    """Qualify all closed framing and text before publishing any counter table."""
    budget.consume("bytes", 4096)
    budget.consume("states", 16)
    budget.consume("work", 32)
    if len(raw) < _FRAME.size:
        raise ResourceError("Missing HTTP hop receipt")
    magic, kind, counter_size, metadata_size, body_size = _FRAME.unpack_from(raw)
    if (
        magic != _MAGIC
        or kind not in (0, 1, 2, 3, 4)
        or not 0 < counter_size <= 1024
        or metadata_size > header_cap
        or body_size > body_cap
        or len(raw) != _FRAME.size + counter_size + metadata_size + body_size
        or kind != 0
        and body_size
    ):
        raise ResourceError("Malformed HTTP hop envelope")
    view = memoryview(raw)
    start = _FRAME.size
    counters = decode_json(
        bytes(view[start : start + counter_size]),
        max_bytes=1024,
        max_depth=4,
        budget=budget,
    )
    position = start + counter_size
    end = position + metadata_size

    def count() -> int:
        nonlocal position
        budget.consume("work")
        if position + 4 > end:
            raise ResourceError("Truncated HTTP hop metadata")
        value = cast(int, _U32.unpack_from(view, position)[0])
        position += 4
        return value

    def text() -> str:
        nonlocal position
        size = count()
        if size > end - position:
            raise ResourceError("Invalid HTTP hop text length")
        budget.consume("work", size + 1)
        budget.consume("bytes", 4 * size + 64)
        budget.consume("states")
        try:
            value = bytes(view[position : position + size]).decode("utf-8")
        except UnicodeError as error:
            raise ResourceError("Invalid HTTP hop text") from error
        position += size
        return value

    effective, target, message = text(), text(), text()
    size = count()
    if size > (end - position) // 8:
        raise ResourceError("Invalid HTTP hop header count")
    budget.consume("states", 2 * size)
    budget.consume("bytes", 128 * size)
    headers = tuple((text(), text()) for _ in range(size))
    if (
        position != end
        or (kind == 0 and (not effective or target or message))
        or (kind == 1 and (not target or message))
        or (kind in (2, 3, 4) and (not message or headers))
    ):
        raise ResourceError("Invalid HTTP hop metadata")
    return HopResponse(
        kind, effective, target, message, headers, bytes(view[end:])
    ), counters


def read_hop(
    url: str, headers: dict[str, str], maximum: int, operation: Operation
) -> HopResponse:
    """Reserve disjoint native/host funds before any network process starts."""
    operation.consume("bytes", 65536)
    operation.consume("states", 64)
    operation.consume("work", 16)
    draft = {
        "url": url,
        "headers": headers,
        "limits": asdict(operation.limits),
        "grants": {kind: operation.remaining(kind) for kind in BUDGET_KINDS},
        "header_cap": operation.limits.max_bytes,
        "body_cap": maximum,
    }
    operation.consume("bytes", 2 * json_bound(draft, operation))
    sample = json.dumps(draft, ensure_ascii=True, separators=(",", ":")).encode()
    request_bound = len(sample) + _REQUEST.size
    # Trusted host-authored input is decoded once in the child. Its maintained
    # parser workspace and traversal are funded here before allocating either.
    operation.consume("bytes", 128 * request_bound + 4096)
    operation.consume("work", 8 * request_bound + 256)
    operation.consume("states", 256)
    native_fixed, host_fixed = 8192, 136 * 1024 + 32768 + 4 * request_bound
    minimum = max(4096, 4 * len(url))
    # Fixed frame/counter pipe storage is separate from semantic metadata.
    pipe_fixed = 5 * (_FRAME.size + 1024 + request_bound)
    body_cap = min(
        maximum,
        2**63 - 1,
        (
            operation.remaining("bytes")
            - native_fixed
            - host_fixed
            - pipe_fixed
            - 23 * minimum
        )
        // 7,
    )
    header_cap = min(
        operation.limits.max_bytes,
        2**32 - 1,
        (
            operation.remaining("bytes")
            - native_fixed
            - host_fixed
            - pipe_fixed
            - 7 * body_cap
        )
        // 23,
        (operation.remaining("work") - 8192) // 8,
    )
    if body_cap < 1 or header_cap < minimum:
        raise LimitExceeded("Insufficient funded HTTP hop capacity")
    native = dict.fromkeys(BUDGET_KINDS, 0)
    host = dict.fromkeys(BUDGET_KINDS, 0)
    native.update(
        {
            "bytes": 2 * body_cap + 8 * header_cap + native_fixed,
            "work": 4 * header_cap + 4096,
            "states": operation.remaining("states") // 2,
        }
    )
    host.update(
        {
            "bytes": 10 * header_cap + host_fixed,
            "work": 4 * header_cap + 4096,
            "states": operation.remaining("states") - native["states"],
        }
    )
    delegated = DelegatedAllowance(operation, native)
    controls = ControlBudget(operation.limits, host)
    host_costs = DelegatedAllowance(operation, host)
    pipe_grants = dict.fromkeys(BUDGET_KINDS, 0)
    pipe_grants["bytes"] = 5 * (header_cap + body_cap) + pipe_fixed
    pipe_costs = DelegatedAllowance(operation, pipe_grants)
    draft.update(grants=delegated.to_data(), header_cap=header_cap, body_cap=body_cap)
    controls.consume("bytes", 4 * request_bound + 512)
    controls.consume("work", 16)
    encoded = json.dumps(draft, ensure_ascii=True, separators=(",", ":")).encode()
    payload = (
        _REQUEST.pack(len(encoded), os.getpid(), int(operation.deadline * 1e9))
        + encoded
    )
    if len(payload) > request_bound:
        raise LimitExceeded("HTTP hop request exceeds funded capture")
    dispatched = received = raw_received = False
    try:
        operation.consume("work", 0)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.workflows.projects.acquisition.http_worker",
            payload,
            cap=max(len(payload), _FRAME.size + 1024 + header_cap + body_cap),
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=operation.cancel,
        )
        copied = dict.fromkeys(BUDGET_KINDS, 0)
        copied["bytes"] = 5 * len(raw)
        pipe_costs.settle(copied)
        raw_received = True
        result, consumed = _receipt(raw, header_cap, body_cap, controls)
        delegated.settle(consumed)
        received = True
        return result
    finally:
        try:
            host_costs.settle(controls.consumed())
        finally:
            try:
                if dispatched and not raw_received:
                    pipe_costs.forfeit()
            finally:
                if dispatched and not received:
                    delegated.forfeit()
